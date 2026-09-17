-- Reaction snapshots retain current choices, never a permanent activity history.
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';
DO $$ BEGIN
    IF (SELECT max(version) FROM msu_hub_private.schema_migrations) IS DISTINCT FROM 3 THEN
        RAISE EXCEPTION 'Reaction storage requires schema revision 3';
    END IF;
END $$;

CREATE TABLE msu_hub_private.reaction_actors (
    bot_id bigint NOT NULL,
    chat_id bigint NOT NULL CHECK (chat_id <> 0),
    message_id bigint NOT NULL CHECK (message_id > 0),
    actor_kind text NOT NULL CHECK (actor_kind IN ('user','chat')),
    actor_id bigint NOT NULL CHECK (actor_id <> 0),
    event_at timestamptz NOT NULL CHECK (isfinite(event_at)),
    update_id bigint NOT NULL,
    -- Transition watermarks settle independently of archive completion order.
    score_at timestamptz CHECK (isfinite(score_at) AND score_at <= event_at),
    score_update_id bigint,
    cleared_at timestamptz CHECK (isfinite(cleared_at) AND cleared_at <= event_at),
    cleared_update_id bigint,
    reactions jsonb NOT NULL CHECK (jsonb_typeof(reactions) = 'array' AND jsonb_array_length(reactions) <= 256),
    PRIMARY KEY (bot_id,chat_id,message_id,actor_kind,actor_id),
    CHECK (actor_kind <> 'user' OR actor_id > 0),
    CHECK ((score_at IS NULL) = (score_update_id IS NULL)),
    CHECK ((cleared_at IS NULL) = (cleared_update_id IS NULL))
);
CREATE INDEX reaction_actors_expiry ON msu_hub_private.reaction_actors(event_at);
CREATE INDEX reaction_actors_chat_window ON msu_hub_private.reaction_actors(bot_id,chat_id,event_at);

CREATE TABLE msu_hub_private.reaction_counts (
    bot_id bigint NOT NULL,
    chat_id bigint NOT NULL CHECK (chat_id <> 0),
    message_id bigint NOT NULL CHECK (message_id > 0),
    event_at timestamptz NOT NULL CHECK (isfinite(event_at)),
    update_id bigint NOT NULL,
    reactions jsonb NOT NULL CHECK (jsonb_typeof(reactions) = 'array' AND jsonb_array_length(reactions) <= 256),
    PRIMARY KEY (bot_id,chat_id,message_id)
);
CREATE INDEX reaction_counts_expiry ON msu_hub_private.reaction_counts(event_at);

ALTER TABLE msu_hub_private.reaction_actors OWNER TO msu_hub_owner;
ALTER TABLE msu_hub_private.reaction_counts OWNER TO msu_hub_owner;
ALTER TABLE msu_hub_private.reaction_actors ENABLE ROW LEVEL SECURITY;
ALTER TABLE msu_hub_private.reaction_counts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON msu_hub_private.reaction_actors,msu_hub_private.reaction_counts FROM PUBLIC,anon,authenticated;

CREATE FUNCTION msu_hub_private.observe_reaction(
    p_value jsonb,p_bot bigint,p_update_id bigint,p_retention_at timestamptz DEFAULT now()
) RETURNS void
LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE
    kind text; chat bigint; message bigint; stamp timestamptz;
    actor bigint; actor_type text; item jsonb; choices jsonb; active boolean; previous_active boolean;
BEGIN
    IF p_value IS NULL OR p_value = 'null'::jsonb THEN RETURN; END IF;
    PERFORM msu_hub_private.check_object(p_value,
        ARRAY['kind','chat_id','message_id','event_at','user_id','actor_chat_id','previous_active','reactions'],
        ARRAY['kind','chat_id','message_id','event_at','reactions']);
    kind := p_value->>'kind';
    IF kind NOT IN ('actor','counts') OR jsonb_typeof(p_value->'kind') <> 'string'
       OR jsonb_typeof(p_value->'chat_id') <> 'number' OR (p_value->>'chat_id') !~ '^-?[0-9]+$'
       OR jsonb_typeof(p_value->'message_id') <> 'number' OR (p_value->>'message_id') !~ '^[0-9]+$'
       OR jsonb_typeof(p_value->'event_at') <> 'string'
       OR jsonb_typeof(p_value->'reactions') <> 'array' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid reaction snapshot';
    END IF;
    chat := (p_value->>'chat_id')::bigint;
    message := (p_value->>'message_id')::bigint;
    stamp := (p_value->>'event_at')::timestamptz;
    IF chat = 0 OR message <= 0 OR NOT isfinite(stamp) OR p_retention_at IS NULL
       OR NOT isfinite(p_retention_at) OR jsonb_array_length(p_value->'reactions') > 256 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid reaction snapshot';
    END IF;
    IF kind = 'actor' THEN
        IF jsonb_typeof(p_value->'previous_active') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Previous reaction state is required';
        END IF;
        previous_active := (p_value->>'previous_active')::boolean;
        IF (p_value->>'user_id' IS NULL) = (p_value->>'actor_chat_id' IS NULL) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Reaction actor is required';
        END IF;
        actor_type := CASE WHEN p_value->>'user_id' IS NOT NULL THEN 'user' ELSE 'chat' END;
        item := CASE WHEN actor_type = 'user' THEN p_value->'user_id' ELSE p_value->'actor_chat_id' END;
        IF jsonb_typeof(item) <> 'number' OR item::text !~ '^-?[0-9]+$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid reaction actor';
        END IF;
        actor := item::text::bigint;
        IF actor = 0 OR (actor_type = 'user' AND actor < 0) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid reaction actor';
        END IF;
    ELSIF p_value->>'user_id' IS NOT NULL OR p_value->>'actor_chat_id' IS NOT NULL OR p_value->>'previous_active' IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Aggregate reactions have no actor';
    END IF;
    FOR item IN SELECT * FROM jsonb_array_elements(p_value->'reactions') LOOP
        PERFORM msu_hub_private.check_object(item,ARRAY['key','count'],ARRAY['key','count']);
        IF jsonb_typeof(item->'key') <> 'string' OR length(item->>'key') > 256
           OR NOT ((item->>'key') = 'paid' OR (left(item->>'key',2) IN ('e:','c:') AND length(item->>'key') > 2))
           OR (item->>'key') ~ '[[:cntrl:]]'
           OR jsonb_typeof(item->'count') <> 'number' OR (item->>'count') !~ '^[0-9]+$'
           OR (item->>'count')::bigint <= 0 OR (kind = 'actor' AND (item->>'count')::bigint <> 1) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid reaction choice';
        END IF;
    END LOOP;
    IF EXISTS(SELECT FROM jsonb_array_elements(p_value->'reactions') x GROUP BY x->>'key' HAVING count(*) > 1) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Duplicate reaction choice';
    END IF;
    SELECT COALESCE(jsonb_agg(x ORDER BY x->>'key'),'[]'::jsonb),COALESCE(bool_or(x->>'key' <> 'paid'),false)
      INTO choices,active FROM jsonb_array_elements(p_value->'reactions') x;
    IF stamp <= p_retention_at - interval '30 days' THEN RETURN; END IF;
    IF kind = 'actor' THEN
        INSERT INTO msu_hub_private.reaction_actors AS existing
            (bot_id,chat_id,message_id,actor_kind,actor_id,event_at,update_id,
                score_at,score_update_id,cleared_at,cleared_update_id,reactions)
        VALUES(p_bot,chat,message,actor_type,actor,stamp,p_update_id,
            CASE WHEN active AND NOT previous_active THEN stamp END,
            CASE WHEN active AND NOT previous_active THEN p_update_id END,
            CASE WHEN NOT active THEN stamp END,CASE WHEN NOT active THEN p_update_id END,choices)
        ON CONFLICT(bot_id,chat_id,message_id,actor_kind,actor_id) DO UPDATE SET
            event_at = greatest(existing.event_at,EXCLUDED.event_at),
            update_id = CASE WHEN (EXCLUDED.event_at,EXCLUDED.update_id) > (existing.event_at,existing.update_id)
                THEN EXCLUDED.update_id ELSE existing.update_id END,
            reactions = CASE WHEN (EXCLUDED.event_at,EXCLUDED.update_id) > (existing.event_at,existing.update_id)
                THEN EXCLUDED.reactions ELSE existing.reactions END,
            score_at = CASE WHEN existing.event_at <= p_retention_at - interval '30 days' OR existing.score_at IS NULL
                    OR (EXCLUDED.score_at,EXCLUDED.score_update_id) > (existing.score_at,existing.score_update_id)
                THEN EXCLUDED.score_at ELSE existing.score_at END,
            score_update_id = CASE WHEN existing.event_at <= p_retention_at - interval '30 days' OR existing.score_at IS NULL
                    OR (EXCLUDED.score_at,EXCLUDED.score_update_id) > (existing.score_at,existing.score_update_id)
                THEN EXCLUDED.score_update_id ELSE existing.score_update_id END,
            cleared_at = CASE WHEN existing.event_at <= p_retention_at - interval '30 days' OR existing.cleared_at IS NULL
                    OR (EXCLUDED.cleared_at,EXCLUDED.cleared_update_id) > (existing.cleared_at,existing.cleared_update_id)
                THEN EXCLUDED.cleared_at ELSE existing.cleared_at END,
            cleared_update_id = CASE WHEN existing.event_at <= p_retention_at - interval '30 days' OR existing.cleared_at IS NULL
                    OR (EXCLUDED.cleared_at,EXCLUDED.cleared_update_id) > (existing.cleared_at,existing.cleared_update_id)
                THEN EXCLUDED.cleared_update_id ELSE existing.cleared_update_id END;
    ELSE
        INSERT INTO msu_hub_private.reaction_counts AS existing(bot_id,chat_id,message_id,event_at,update_id,reactions)
        VALUES(p_bot,chat,message,stamp,p_update_id,choices)
        ON CONFLICT(bot_id,chat_id,message_id) DO UPDATE SET
            event_at = EXCLUDED.event_at, update_id = EXCLUDED.update_id, reactions = EXCLUDED.reactions
        WHERE (EXCLUDED.event_at,EXCLUDED.update_id) > (existing.event_at,existing.update_id);
    END IF;
END;
$$;
ALTER FUNCTION msu_hub_private.observe_reaction(jsonb,bigint,bigint,timestamptz) OWNER TO msu_hub_owner;
REVOKE ALL ON FUNCTION msu_hub_private.observe_reaction(jsonb,bigint,bigint,timestamptz) FROM PUBLIC,anon,authenticated;

CREATE OR REPLACE FUNCTION msu_hub_api.archive_update_v1(p_update jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    bot bigint := msu_hub_private.require_principal();
    receipt uuid;
    received timestamptz := COALESCE((p_update->>'received_at')::timestamptz,now());
BEGIN
    PERFORM msu_hub_private.check_object(p_update,
        ARRAY['id','update_id','received_at','kind','handled','data','users','chats','memberships','topics','messages','reaction'],
        ARRAY['id','update_id','kind','handled','data']);
    INSERT INTO msu_hub_private.updates(id,created,data,handled,bot_id,update_id,kind)
    VALUES((p_update->>'id')::uuid,received,p_update->'data',(p_update->>'handled')::boolean,bot,
        (p_update->>'update_id')::bigint,p_update->>'kind')
    ON CONFLICT(bot_id,update_id) WHERE NOT is_legacy DO NOTHING RETURNING id INTO receipt;
    IF receipt IS NULL THEN RETURN; END IF;
    PERFORM msu_hub_private.observe_archive(p_update,bot,receipt,received);
    PERFORM msu_hub_private.observe_reaction(p_update->'reaction',bot,(p_update->>'update_id')::bigint);
END;
$$;
ALTER FUNCTION msu_hub_api.archive_update_v1(jsonb) OWNER TO msu_hub_owner;
REVOKE ALL ON FUNCTION msu_hub_api.archive_update_v1(jsonb) FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION msu_hub_api.archive_update_v1(jsonb) TO authenticated;

CREATE FUNCTION msu_hub_api.reaction_scoreboard_v1(p_chat_id bigint,p_days integer DEFAULT 30,p_limit integer DEFAULT 10)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    bot bigint := msu_hub_private.require_principal();
    cutoff timestamptz := now() - interval '30 days';
    since timestamptz;
    result jsonb;
BEGIN
    IF p_chat_id IS NULL OR p_chat_id = 0 OR p_days IS NULL OR p_days NOT IN (1,7,30)
       OR p_limit IS NULL OR p_limit < 1 OR p_limit > 10 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid reaction statistics window';
    END IF;
    since := now() - make_interval(days => p_days);
    WITH actors AS MATERIALIZED (
        SELECT * FROM msu_hub_private.reaction_actors
        WHERE bot_id = bot AND chat_id = p_chat_id AND event_at > cutoff AND event_at <= now()
    ), latest_actor AS (
        SELECT DISTINCT ON(message_id) message_id,event_at,update_id FROM actors
        ORDER BY message_id,event_at DESC,update_id DESC
    ), counts AS MATERIALIZED (
        SELECT * FROM msu_hub_private.reaction_counts
        WHERE bot_id = bot AND chat_id = p_chat_id AND event_at > cutoff AND event_at <= now()
    ), named AS MATERIALIZED (
        SELECT a.*,c.event_at AS anonymous_at,c.update_id AS anonymous_update_id
        FROM actors a LEFT JOIN counts c USING(message_id)
        WHERE c.message_id IS NULL OR (a.event_at,a.update_id) > (c.event_at,c.update_id)
    ), anonymous AS MATERIALIZED (
        SELECT c.* FROM counts c LEFT JOIN latest_actor l USING(message_id)
        WHERE c.event_at > since AND (l.message_id IS NULL OR (c.event_at,c.update_id) >= (l.event_at,l.update_id))
    ), points AS MATERIALIZED (
        SELECT a.actor_id,a.message_id,a.reactions,
            CASE WHEN m.sender_chat_id IS NULL AND NOT author.is_bot THEN author.user_id END AS author_id,
            m.thread_id
        FROM named a JOIN msu_hub_private.users giver ON giver.user_id = a.actor_id AND NOT giver.is_bot
        LEFT JOIN msu_hub_private.messages m ON m.bot_id = bot AND m.chat_id = p_chat_id
            AND m.message_id = a.message_id AND m.business_connection_id = '' AND m.sent_at > cutoff
        LEFT JOIN msu_hub_private.users author ON author.user_id = m.sender_user_id
        WHERE a.actor_kind = 'user' AND a.score_at > since
            AND (a.cleared_at IS NULL OR (a.score_at,a.score_update_id) > (a.cleared_at,a.cleared_update_id))
            -- Aggregate mode cannot prove that an individual's selection survived.
            AND (a.anonymous_at IS NULL OR (a.score_at,a.score_update_id) > (a.anonymous_at,a.anonymous_update_id))
            AND EXISTS(SELECT FROM jsonb_array_elements(a.reactions) x WHERE x->>'key' <> 'paid')
            AND NOT (m.sender_chat_id IS NULL AND COALESCE(m.sender_user_id = a.actor_id,false))
    ), giver_ranks AS (
        SELECT actor_id AS user_id,count(*) AS score,count(DISTINCT author_id) AS people,count(DISTINCT message_id) AS messages
        FROM points GROUP BY actor_id
    ), getter_ranks AS (
        SELECT author_id AS user_id,count(*) AS score,count(DISTINCT actor_id) AS people,count(DISTINCT message_id) AS messages
        FROM points WHERE author_id IS NOT NULL GROUP BY author_id
    ), top_givers AS (
        SELECT r.*,u.first_name,u.last_name,u.username FROM giver_ranks r JOIN msu_hub_private.users u USING(user_id)
        ORDER BY score DESC,people DESC,messages DESC,user_id LIMIT p_limit
    ), top_getters AS (
        SELECT r.*,u.first_name,u.last_name,u.username FROM getter_ranks r JOIN msu_hub_private.users u USING(user_id)
        ORDER BY score DESC,people DESC,messages DESC,user_id LIMIT p_limit
    ), human_emoji AS MATERIALIZED (
        SELECT x->>'key' AS key,count(*) AS count FROM points CROSS JOIN LATERAL jsonb_array_elements(reactions) x
        WHERE x->>'key' <> 'paid' GROUP BY x->>'key'
    ), top_emoji AS (
        SELECT * FROM human_emoji ORDER BY count DESC,key LIMIT p_limit
    ), top_posts AS (
        SELECT message_id,thread_id,author_id,count(*) AS score,count(DISTINCT actor_id) AS people
        FROM points GROUP BY message_id,thread_id,author_id ORDER BY score DESC,people DESC,message_id DESC LIMIT p_limit
    ), anonymous_emoji AS MATERIALIZED (
        SELECT x->>'key' AS key,(x->>'count')::bigint AS count
        FROM anonymous CROSS JOIN LATERAL jsonb_array_elements(reactions) x
    ), extra_named AS MATERIALIZED (
        SELECT a.actor_kind,x->>'key' AS key,(x->>'count')::bigint AS count
        FROM named a CROSS JOIN LATERAL jsonb_array_elements(reactions) x WHERE a.event_at > since
    )
    SELECT jsonb_build_object(
        'days',p_days,
        'getters',COALESCE((SELECT jsonb_agg(to_jsonb(r) ORDER BY score DESC,people DESC,messages DESC,user_id) FROM top_getters r),'[]'::jsonb),
        'givers',COALESCE((SELECT jsonb_agg(to_jsonb(r) ORDER BY score DESC,people DESC,messages DESC,user_id) FROM top_givers r),'[]'::jsonb),
        'emoji',COALESCE((SELECT jsonb_agg(to_jsonb(r) ORDER BY count DESC,key) FROM top_emoji r),'[]'::jsonb),
        'posts',COALESCE((SELECT jsonb_agg(to_jsonb(r) ORDER BY score DESC,people DESC,message_id DESC) FROM top_posts r),'[]'::jsonb),
        'summary',jsonb_build_object(
            'points',(SELECT count(*) FROM points),
            'reactions',COALESCE((SELECT sum(count) FROM human_emoji),0),
            'givers',(SELECT count(*) FROM giver_ranks),
            'getters',(SELECT count(*) FROM getter_ranks),
            'messages',(SELECT count(DISTINCT message_id) FROM points),
            'unattributed',(SELECT count(*) FROM points WHERE author_id IS NULL),
            'anonymous',COALESCE((SELECT sum(count) FROM anonymous_emoji WHERE key <> 'paid'),0),
            'paid',COALESCE((SELECT sum(count) FROM anonymous_emoji WHERE key = 'paid'),0)
                + COALESCE((SELECT sum(count) FROM extra_named WHERE key = 'paid'),0),
            'channel_reactions',COALESCE((SELECT sum(count) FROM extra_named WHERE actor_kind = 'chat' AND key <> 'paid'),0)
        )
    ) INTO result;
    RETURN result;
END;
$$;
ALTER FUNCTION msu_hub_api.reaction_scoreboard_v1(bigint,integer,integer) OWNER TO msu_hub_owner;
REVOKE ALL ON FUNCTION msu_hub_api.reaction_scoreboard_v1(bigint,integer,integer) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION msu_hub_api.reaction_scoreboard_v1(bigint,integer,integer) TO authenticated;

-- Every expiring relation has an independent clock, batch and no inbound cascade.
CREATE OR REPLACE FUNCTION msu_hub_private.retain_messages(p_batch integer DEFAULT 1000,p_now timestamptz DEFAULT now()) RETURNS jsonb
LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE
    message_count integer; update_count integer; actor_count integer; aggregate_count integer;
    cutoff timestamptz := p_now - interval '30 days';
BEGIN
    IF p_batch IS NULL OR p_batch < 1 OR p_batch > 10000 OR p_now IS NULL OR NOT isfinite(p_now) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid retention batch';
    END IF;
    WITH expired AS (SELECT ctid FROM msu_hub_private.messages WHERE sent_at <= cutoff ORDER BY sent_at LIMIT p_batch FOR UPDATE SKIP LOCKED)
    DELETE FROM msu_hub_private.messages WHERE ctid IN (SELECT ctid FROM expired);
    GET DIAGNOSTICS message_count = ROW_COUNT;
    WITH expired AS (SELECT id FROM msu_hub_private.updates WHERE created <= cutoff ORDER BY created LIMIT p_batch FOR UPDATE SKIP LOCKED)
    DELETE FROM msu_hub_private.updates WHERE id IN (SELECT id FROM expired);
    GET DIAGNOSTICS update_count = ROW_COUNT;
    WITH expired AS (SELECT ctid FROM msu_hub_private.reaction_actors WHERE event_at <= cutoff ORDER BY event_at LIMIT p_batch FOR UPDATE SKIP LOCKED)
    DELETE FROM msu_hub_private.reaction_actors WHERE ctid IN (SELECT ctid FROM expired);
    GET DIAGNOSTICS actor_count = ROW_COUNT;
    WITH expired AS (SELECT ctid FROM msu_hub_private.reaction_counts WHERE event_at <= cutoff ORDER BY event_at LIMIT p_batch FOR UPDATE SKIP LOCKED)
    DELETE FROM msu_hub_private.reaction_counts WHERE ctid IN (SELECT ctid FROM expired);
    GET DIAGNOSTICS aggregate_count = ROW_COUNT;
    RETURN jsonb_build_object('messages',message_count,'updates',update_count,
        'reaction_actors',actor_count,'reaction_counts',aggregate_count,'cutoff',cutoff);
END;
$$;
ALTER FUNCTION msu_hub_private.retain_messages(integer,timestamptz) OWNER TO msu_hub_owner;
REVOKE ALL ON FUNCTION msu_hub_private.retain_messages(integer,timestamptz) FROM PUBLIC,anon,authenticated;

INSERT INTO msu_hub_private.schema_migrations(version) VALUES(4);
NOTIFY pgrst, 'reload schema';
COMMIT;
