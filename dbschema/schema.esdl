module meta {
    abstract type HasCreated {
        required property created -> datetime {
            default := std::datetime_current();
        }
        index on (.created);
    }

    abstract type HasMetadata {
        required property metadata -> json {
            default := <json>'{}';
        }
    }
}

module telegram {
    type BotUpdate extending meta::HasCreated {
        annotation title := 'Telegram update';
        annotation description := 'This object represents an incoming update';

        required property data -> json;
        required property handled -> bool {
            default := false;
        }
    }

    type User extending meta::HasCreated, meta::HasMetadata {
        annotation title := 'Telegram user';
        annotation description := 'This object represents a Telegram user or bot';

        required property user_id -> int64 {
            constraint exclusive;
        }
        required property is_bot -> bool;
        required property first_name -> str;

        property last_name -> str;
        property username -> str;
        property language_code -> str;

        # Computed
        property full_name := .first_name ++ ' ' ++ .last_name if exists .last_name else .first_name;
    }

    type Chat extending meta::HasCreated, meta::HasMetadata {
        annotation title := 'Telegram chat';
        annotation description := 'This object represents a chat';

        required property chat_id -> int64 {
            constraint exclusive;
        }
        required property type -> str;

        property title -> str;
        property username -> str;
        property first_name -> str;
        property last_name -> str;

        # Computed
        property full_name := .title if exists .title else (.first_name ++ ' ' ++ .last_name if exists .last_name else .first_name);
    }
}

module msu_hub {
    type EcosystemChat extending meta::HasCreated {
        required property chat_id -> int64 {
            constraint exclusive;
        }

        required property name -> str;
        required property section -> str;
        required property is_hidden -> bool {
            default := false;
        }

        property username_alias -> str {
            default := '';
        }
        property members -> int32;
        property pinned_message_id -> int32;
    }
}

module vk_tg {
    type VkWallPosting extending meta::HasCreated {
        required property owner_id -> int64;
        required property chat_id -> int64;
        constraint exclusive on ( (.owner_id, .chat_id) );

        required property last_post_id -> int32 {
            default := 0;
        }
        required property with_reposts -> bool {
            default := false;
        }
        required property with_header -> bool {
            default := true;
        }
        required property is_suspended -> bool {
            default := false;
        }

        property description -> str;
    }
}
