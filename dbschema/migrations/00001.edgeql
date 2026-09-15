CREATE MIGRATION m1ltyxg4huggzrfgbvj2k653woquxjqfm675uwpdonlfyw4b3b44zq
    ONTO initial
{
  CREATE MODULE meta IF NOT EXISTS;
  CREATE MODULE telegram IF NOT EXISTS;
  CREATE ABSTRACT TYPE meta::HasCreated {
      CREATE REQUIRED PROPERTY created -> std::datetime {
          SET default := (std::datetime_current());
      };
  };
  CREATE TYPE telegram::BotUpdate EXTENDING meta::HasCreated {
      CREATE ANNOTATION std::description := 'This object represents an incoming update';
      CREATE ANNOTATION std::title := 'Telegram update';
      CREATE REQUIRED PROPERTY data -> std::json;
      CREATE REQUIRED PROPERTY handled -> std::bool {
          SET default := false;
      };
  };
  CREATE ABSTRACT TYPE meta::HasMetadata {
      CREATE REQUIRED PROPERTY metadata -> std::json {
          SET default := (<std::json>{});
      };
  };
  CREATE TYPE telegram::Chat EXTENDING meta::HasCreated, meta::HasMetadata {
      CREATE ANNOTATION std::description := 'This object represents a chat';
      CREATE ANNOTATION std::title := 'Telegram chat';
      CREATE REQUIRED PROPERTY chat_id -> std::int64 {
          CREATE CONSTRAINT std::exclusive;
      };
      CREATE PROPERTY first_name -> std::str;
      CREATE PROPERTY last_name -> std::str;
      CREATE PROPERTY title -> std::str;
      CREATE REQUIRED PROPERTY type -> std::str;
      CREATE PROPERTY username -> std::str;
  };
  CREATE TYPE telegram::User EXTENDING meta::HasCreated, meta::HasMetadata {
      CREATE ANNOTATION std::description := 'This object represents a Telegram user or bot';
      CREATE ANNOTATION std::title := 'Telegram user';
      CREATE REQUIRED PROPERTY first_name -> std::str;
      CREATE PROPERTY last_name -> std::str;
      CREATE PROPERTY full_name := ((((.first_name ++ ' ') ++ .last_name) IF EXISTS (.last_name) ELSE .first_name));
      CREATE REQUIRED PROPERTY is_bot -> std::bool;
      CREATE PROPERTY language_code -> std::str;
      CREATE REQUIRED PROPERTY user_id -> std::int64 {
          CREATE CONSTRAINT std::exclusive;
      };
      CREATE PROPERTY username -> std::str;
  };
};
