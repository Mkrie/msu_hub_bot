-- Membership evidence has its own clock; activity does not assert current presence.
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';
DO $$ BEGIN
    IF (SELECT max(version) FROM msu_hub_private.schema_migrations) IS DISTINCT FROM 8 THEN
        RAISE EXCEPTION 'Membership evidence requires schema revision 8';
    END IF;
END $$;

-- Do not fabricate status dates from last activity for previously stored rows.
ALTER TABLE msu_hub_private.chat_users
    ADD COLUMN status_observed_at timestamptz,
    ADD COLUMN status_source text CHECK (status_source IN ('legacy','chat_member','my_chat_member','service_join','service_leave')),
    ADD COLUMN status_event_id bigint,
    ADD COLUMN observation_source text CHECK (observation_source IN ('message','reply','callback','reaction','join_request','membership')),
    ADD COLUMN admin_lost_at timestamptz;

CREATE FUNCTION msu_hub_private.membership_state(p_status text,p_permissions jsonb,p_at timestamptz) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path = '' AS $$
    SELECT CASE
        WHEN p_at IS NULL THEN 'unknown'
        WHEN p_status IN ('creator','administrator','member') THEN 'present'
        WHEN p_status IN ('left','kicked') THEN 'absent'
        WHEN p_status='restricted' AND p_permissions->'is_member'='true'::jsonb THEN 'present'
        WHEN p_status='restricted' AND p_permissions->'is_member'='false'::jsonb THEN 'absent'
        ELSE 'unknown' END
$$;
ALTER FUNCTION msu_hub_private.membership_state(text,jsonb,timestamptz) OWNER TO msu_hub_owner;
REVOKE ALL ON FUNCTION msu_hub_private.membership_state(text,jsonb,timestamptz) FROM PUBLIC,anon,authenticated;

CREATE INDEX chat_users_state_page ON msu_hub_private.chat_users
    (chat_id,msu_hub_private.membership_state(status,permissions,status_observed_at),user_id);

CREATE FUNCTION msu_hub_private.membership_status_newer(
    p_at timestamptz,p_source text,p_event bigint,p_old_at timestamptz,p_old_source text,p_old_event bigint
) RETURNS boolean LANGUAGE sql IMMUTABLE SET search_path = '' AS $$
    SELECT p_at IS NOT NULL AND (p_old_at IS NULL OR
        (p_at,CASE p_source WHEN 'chat_member' THEN 2 WHEN 'my_chat_member' THEN 2 WHEN 'legacy' THEN 0 ELSE 1 END,
            COALESCE(p_event,'-9223372036854775808'::bigint)) >
        (p_old_at,CASE p_old_source WHEN 'chat_member' THEN 2 WHEN 'my_chat_member' THEN 2 WHEN 'legacy' THEN 0 ELSE 1 END,
            COALESCE(p_old_event,'-9223372036854775808'::bigint)))
$$;
ALTER FUNCTION msu_hub_private.membership_status_newer(timestamptz,text,bigint,timestamptz,text,bigint) OWNER TO msu_hub_owner;
REVOKE ALL ON FUNCTION msu_hub_private.membership_status_newer(timestamptz,text,bigint,timestamptz,text,bigint) FROM PUBLIC,anon,authenticated;

CREATE FUNCTION msu_hub_private.observe_membership(item jsonb,p_bot bigint,p_received timestamptz,p_update_id bigint) RETURNS void
LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE stamp timestamptz; status_at timestamptz; source text; event_id bigint; loss timestamptz;
BEGIN
    PERFORM msu_hub_private.check_object(item,
        ARRAY['chat_id','user_id','observed_at','observation_source','status','permissions','status_observed_at','status_source','status_event_id','admin_lost_at'],
        ARRAY['chat_id','user_id']);
    stamp := COALESCE((item->>'observed_at')::timestamptz,p_received);
    source := item->>'status_source';
    loss := (item->>'admin_lost_at')::timestamptz;
    IF NOT isfinite(stamp) OR (loss IS NOT NULL AND (NOT isfinite(loss) OR (item->>'user_id')::bigint<>p_bot OR source IS DISTINCT FROM 'my_chat_member')) THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='Invalid membership evidence';
    END IF;
    IF (source IS NOT NULL OR item->>'status_observed_at' IS NOT NULL OR item->>'status_event_id' IS NOT NULL)
        AND (source IS NULL OR source NOT IN ('chat_member','my_chat_member','service_join','service_leave')
        OR item->>'status' IS NULL OR item->>'status_observed_at' IS NULL OR item->>'status_event_id' IS NULL) THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='Incomplete membership evidence';
    END IF;
    IF source='my_chat_member' AND (item->>'user_id')::bigint<>p_bot THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='Invalid bot membership evidence';
    END IF;
    IF item->>'status' IS NOT NULL THEN
        IF source IS NOT NULL AND item->>'status' NOT IN ('creator','administrator','member','restricted','left','kicked') THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='Invalid membership status';
        END IF;
        status_at := COALESCE((item->>'status_observed_at')::timestamptz,stamp);
        source := COALESCE(source,'legacy');
        event_id := COALESCE((item->>'status_event_id')::bigint,p_update_id);
        IF NOT isfinite(status_at) THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='Invalid membership status timestamp';
        END IF;
    ELSIF item->>'status_observed_at' IS NOT NULL OR item->>'status_event_id' IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='Status clock requires membership status';
    END IF;
    INSERT INTO msu_hub_private.chat_users AS existing
        (chat_id,user_id,first_seen_at,last_seen_at,observation_source,status,permissions,status_observed_at,status_source,status_event_id,admin_lost_at)
    VALUES ((item->>'chat_id')::bigint,(item->>'user_id')::bigint,least(stamp,status_at),greatest(stamp,status_at),item->>'observation_source',item->>'status',
        CASE WHEN status_at IS NULL THEN '{}'::jsonb ELSE COALESCE(item->'permissions','{}'::jsonb) END,status_at,source,event_id,loss)
    ON CONFLICT(chat_id,user_id) DO UPDATE SET
        first_seen_at=least(existing.first_seen_at,EXCLUDED.first_seen_at),
        last_seen_at=greatest(existing.last_seen_at,EXCLUDED.last_seen_at),
        observation_source=CASE WHEN EXCLUDED.last_seen_at>existing.last_seen_at THEN EXCLUDED.observation_source
            WHEN EXCLUDED.last_seen_at=existing.last_seen_at AND existing.observation_source IS NULL
            THEN EXCLUDED.observation_source ELSE existing.observation_source END,
        admin_lost_at=greatest(existing.admin_lost_at,EXCLUDED.admin_lost_at),
        (status,permissions,status_observed_at,status_source,status_event_id)=(
            SELECT CASE WHEN newer THEN EXCLUDED.status ELSE existing.status END,
                CASE WHEN newer THEN EXCLUDED.permissions ELSE existing.permissions END,
                CASE WHEN newer THEN EXCLUDED.status_observed_at ELSE existing.status_observed_at END,
                CASE WHEN newer THEN EXCLUDED.status_source ELSE existing.status_source END,
                CASE WHEN newer THEN EXCLUDED.status_event_id ELSE existing.status_event_id END
            FROM (SELECT msu_hub_private.membership_status_newer(EXCLUDED.status_observed_at,EXCLUDED.status_source,EXCLUDED.status_event_id,
                existing.status_observed_at,existing.status_source,existing.status_event_id) AS newer) decision);
END $$;
ALTER FUNCTION msu_hub_private.observe_membership(jsonb,bigint,timestamptz,bigint) OWNER TO msu_hub_owner;
REVOKE ALL ON FUNCTION msu_hub_private.observe_membership(jsonb,bigint,timestamptz,bigint) FROM PUBLIC,anon,authenticated;

CREATE FUNCTION msu_hub_api.observe_memberships_v1(p_observation jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE bot bigint := msu_hub_private.require_principal(); item jsonb; field text; received timestamptz;
BEGIN
    PERFORM msu_hub_private.check_object(p_observation,ARRAY['update_id','received_at','users','chats','memberships'],ARRAY['update_id']);
    IF octet_length(p_observation::text)>1048576 OR jsonb_typeof(p_observation->'update_id')<>'number' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='Invalid membership batch';
    END IF;
    received := COALESCE((p_observation->>'received_at')::timestamptz,now());
    IF NOT isfinite(received) THEN RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='Invalid membership batch'; END IF;
    FOREACH field IN ARRAY ARRAY['users','chats','memberships'] LOOP
        IF p_observation ? field AND (jsonb_typeof(p_observation->field) IS DISTINCT FROM 'array'
            OR jsonb_array_length(p_observation->field)>512) THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='Invalid membership batch';
        END IF;
    END LOOP;
    FOR item IN SELECT * FROM jsonb_array_elements(COALESCE(p_observation->'users','[]'::jsonb)) LOOP
        PERFORM msu_hub_private.observe_user(item);
    END LOOP;
    FOR item IN SELECT * FROM jsonb_array_elements(COALESCE(p_observation->'chats','[]'::jsonb)) LOOP
        PERFORM msu_hub_private.observe_chat(item);
    END LOOP;
    FOR item IN SELECT * FROM jsonb_array_elements(COALESCE(p_observation->'memberships','[]'::jsonb)) LOOP
        PERFORM msu_hub_private.observe_membership(item,bot,received,(p_observation->>'update_id')::bigint);
    END LOOP;
END $$;
ALTER FUNCTION msu_hub_api.observe_memberships_v1(jsonb) OWNER TO msu_hub_owner;
REVOKE ALL ON FUNCTION msu_hub_api.observe_memberships_v1(jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION msu_hub_api.observe_memberships_v1(jsonb) TO authenticated;

-- Keep archive receipt, topic and message behavior unchanged.
CREATE OR REPLACE FUNCTION msu_hub_private.observe_archive(
    p_update jsonb, p_bot bigint, p_receipt uuid, p_received timestamptz,
    p_retention_at timestamptz DEFAULT now()
) RETURNS void
LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE item jsonb; stamp timestamptz;
BEGIN
    IF p_retention_at IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Retention timestamp is required';
    END IF;
    FOR item IN SELECT * FROM jsonb_array_elements(COALESCE(p_update->'users','[]'::jsonb)) LOOP
        PERFORM msu_hub_private.observe_user(item);
    END LOOP;
    FOR item IN SELECT * FROM jsonb_array_elements(COALESCE(p_update->'chats','[]'::jsonb)) LOOP
        PERFORM msu_hub_private.observe_chat(item);
    END LOOP;
    FOR item IN SELECT * FROM jsonb_array_elements(COALESCE(p_update->'memberships','[]'::jsonb)) LOOP
        PERFORM msu_hub_private.observe_membership(item,p_bot,p_received,(p_update->>'update_id')::bigint);
    END LOOP;
    FOR item IN SELECT * FROM jsonb_array_elements(COALESCE(p_update->'topics','[]'::jsonb)) LOOP
        PERFORM msu_hub_private.check_object(item, ARRAY['chat_id','thread_id','observed_at','title','is_closed','profile'], ARRAY['chat_id','thread_id']);
        stamp := COALESCE((item->>'observed_at')::timestamptz,p_received);
        INSERT INTO msu_hub_private.chat_topics AS existing(chat_id,thread_id,first_seen_at,last_seen_at,title,is_closed,profile)
        VALUES ((item->>'chat_id')::bigint,(item->>'thread_id')::bigint,stamp,stamp,item->>'title',(item->>'is_closed')::boolean,COALESCE(item->'profile','{}'::jsonb))
        ON CONFLICT(chat_id,thread_id) DO UPDATE SET
            first_seen_at = least(existing.first_seen_at,stamp), last_seen_at = greatest(existing.last_seen_at,stamp),
            title = CASE WHEN stamp >= existing.last_seen_at AND item ? 'title' THEN EXCLUDED.title ELSE existing.title END,
            is_closed = CASE WHEN stamp >= existing.last_seen_at AND item ? 'is_closed' THEN EXCLUDED.is_closed ELSE existing.is_closed END,
            profile = CASE WHEN stamp >= existing.last_seen_at THEN existing.profile || EXCLUDED.profile ELSE existing.profile END;
    END LOOP;
    FOR item IN SELECT * FROM jsonb_array_elements(COALESCE(p_update->'messages','[]'::jsonb)) LOOP
        PERFORM msu_hub_private.check_object(item, ARRAY['chat_id','message_id','sent_at','edited_at','observed_at','business_connection_id','sender_user_id','sender_chat_id','thread_id','reply_to_message_id','data'], ARRAY['chat_id','message_id','sent_at','data']);
        IF (item->>'sent_at')::timestamptz <= p_retention_at - interval '30 days' THEN CONTINUE; END IF;
        INSERT INTO msu_hub_private.messages AS existing(bot_id,chat_id,message_id,business_connection_id,sent_at,edited_at,observed_at,sender_user_id,sender_chat_id,thread_id,reply_to_message_id,source_update_id,data)
        VALUES (p_bot,(item->>'chat_id')::bigint,(item->>'message_id')::bigint,COALESCE(item->>'business_connection_id',''),(item->>'sent_at')::timestamptz,
            (item->>'edited_at')::timestamptz,COALESCE((item->>'observed_at')::timestamptz,p_received),(item->>'sender_user_id')::bigint,(item->>'sender_chat_id')::bigint,
            (item->>'thread_id')::bigint,(item->>'reply_to_message_id')::bigint,p_receipt,item->'data')
        ON CONFLICT(bot_id,chat_id,message_id,business_connection_id) DO UPDATE SET
            sent_at = least(existing.sent_at,EXCLUDED.sent_at), edited_at = EXCLUDED.edited_at, observed_at = EXCLUDED.observed_at,
            sender_user_id = EXCLUDED.sender_user_id, sender_chat_id = EXCLUDED.sender_chat_id, thread_id = EXCLUDED.thread_id,
            reply_to_message_id = EXCLUDED.reply_to_message_id, source_update_id = EXCLUDED.source_update_id, data = EXCLUDED.data
        WHERE (COALESCE(EXCLUDED.edited_at,EXCLUDED.sent_at), EXCLUDED.observed_at)
            >= (COALESCE(existing.edited_at,existing.sent_at), existing.observed_at);
    END LOOP;
END;
$$;
ALTER FUNCTION msu_hub_private.observe_archive(jsonb,bigint,uuid,timestamptz,timestamptz) OWNER TO msu_hub_owner;
REVOKE ALL ON FUNCTION msu_hub_private.observe_archive(jsonb,bigint,uuid,timestamptz,timestamptz) FROM PUBLIC, anon, authenticated;

CREATE FUNCTION msu_hub_api.list_chat_members_v1(
    p_chat_id bigint,p_state text DEFAULT 'present',p_after_user_id bigint DEFAULT NULL,p_limit integer DEFAULT 50
) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE bot bigint := msu_hub_private.require_principal(); members jsonb; cursor_id bigint; coverage jsonb;
BEGIN
    IF p_chat_id IS NULL OR p_chat_id=0 OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100
        OR (p_state IS NOT NULL AND p_state NOT IN ('present','absent','unknown')) THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='Invalid membership query';
    END IF;
    WITH selected AS MATERIALIZED (
        SELECT c.*,u.is_bot,u.first_name,u.last_name,u.username,
            msu_hub_private.membership_state(c.status,c.permissions,c.status_observed_at) AS state
        FROM msu_hub_private.chat_users c JOIN msu_hub_private.users u USING(user_id)
        WHERE c.chat_id=p_chat_id AND (p_after_user_id IS NULL OR c.user_id>p_after_user_id)
          AND (p_state IS NULL OR msu_hub_private.membership_state(c.status,c.permissions,c.status_observed_at)=p_state)
        ORDER BY c.user_id LIMIT p_limit+1
    ), page AS (SELECT * FROM selected ORDER BY user_id LIMIT p_limit)
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'chat_id',chat_id,'user_id',user_id,'is_bot',is_bot,'first_name',first_name,'last_name',last_name,'username',username,
        'state',state,'status',status,'status_observed_at',status_observed_at,'status_source',status_source,'status_event_id',status_event_id,
        'is_member',CASE WHEN jsonb_typeof(permissions->'is_member')='boolean' THEN permissions->'is_member' ELSE NULL END,
        'observation_source',observation_source,'first_seen_at',first_seen_at,'last_seen_at',last_seen_at) ORDER BY user_id),'[]'::jsonb),
        CASE WHEN (SELECT count(*) FROM selected)>p_limit THEN max(user_id) ELSE NULL END
    INTO members,cursor_id FROM page;
    SELECT jsonb_build_object('complete',false,
        'observed_count',counts.observed,'present_count',counts.present,'absent_count',counts.absent,'unknown_count',counts.unknown,
        'bot_state',msu_hub_private.membership_state(c.status,c.permissions,c.status_observed_at),
        'bot_status',c.status,'bot_status_observed_at',c.status_observed_at,'bot_status_source',c.status_source,
        'bot_is_admin',CASE WHEN msu_hub_private.membership_state(c.status,c.permissions,c.status_observed_at)<>'unknown'
            THEN c.status IN ('creator','administrator') ELSE NULL END,
        'admin_lost_at',c.admin_lost_at)
    INTO coverage FROM (SELECT 1) singleton LEFT JOIN msu_hub_private.chat_users c ON c.chat_id=p_chat_id AND c.user_id=bot
    CROSS JOIN (SELECT count(*) AS observed,
        count(*) FILTER (WHERE msu_hub_private.membership_state(status,permissions,status_observed_at)='present') AS present,
        count(*) FILTER (WHERE msu_hub_private.membership_state(status,permissions,status_observed_at)='absent') AS absent,
        count(*) FILTER (WHERE msu_hub_private.membership_state(status,permissions,status_observed_at)='unknown') AS unknown
        FROM msu_hub_private.chat_users WHERE chat_id=p_chat_id) counts;
    RETURN jsonb_build_object('chat_id',p_chat_id,'state',p_state,'members',members,'next_after_user_id',cursor_id,'coverage',coverage);
END $$;
ALTER FUNCTION msu_hub_api.list_chat_members_v1(bigint,text,bigint,integer) OWNER TO msu_hub_owner;
REVOKE ALL ON FUNCTION msu_hub_api.list_chat_members_v1(bigint,text,bigint,integer) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION msu_hub_api.list_chat_members_v1(bigint,text,bigint,integer) TO authenticated;

CREATE OR REPLACE FUNCTION msu_hub_api.health_v1() RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE bot bigint := msu_hub_private.require_principal();
BEGIN
    RETURN jsonb_build_object('schema_version',1,'bot_id',bot,'application_documents',1,'memberships',1);
END $$;

INSERT INTO msu_hub_private.schema_migrations(version) VALUES(9);
NOTIFY pgrst, 'reload schema';
COMMIT;
