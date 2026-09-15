CREATE MIGRATION m1uwql657kosknc2q3pvvrmfcagsblsf7rr7a35lxyokjp7jl47mpq
    ONTO m1j2gzqy6k3vpucseyofs2ks2r2m7v33bnd7u52htgke3b74nkmhia
{
  CREATE MODULE msu_hub IF NOT EXISTS;
  CREATE MODULE vk_tg IF NOT EXISTS;
  CREATE TYPE msu_hub::EcosystemChat EXTENDING meta::HasCreated {
      CREATE REQUIRED PROPERTY chat_id -> std::int64 {
          CREATE CONSTRAINT std::exclusive;
      };
      CREATE PROPERTY is_hidden -> std::bool {
          SET default := false;
      };
      CREATE PROPERTY links_message_id -> std::int32;
      CREATE PROPERTY members -> std::int32;
      CREATE PROPERTY name -> std::str;
      CREATE PROPERTY section -> std::str;
      CREATE PROPERTY username_alias -> std::str {
          SET default := '';
      };
  };
  CREATE TYPE vk_tg::VkWallPosting EXTENDING meta::HasCreated {
      CREATE REQUIRED PROPERTY chat_id -> std::int64 {
          CREATE CONSTRAINT std::exclusive;
      };
      CREATE PROPERTY description -> std::str;
      CREATE REQUIRED PROPERTY is_suspended -> std::bool {
          SET default := false;
      };
      CREATE REQUIRED PROPERTY last_post_id -> std::int32 {
          SET default := 0;
      };
      CREATE REQUIRED PROPERTY owner_id -> std::int64 {
          CREATE CONSTRAINT std::exclusive;
      };
      CREATE REQUIRED PROPERTY with_header -> std::bool {
          SET default := true;
      };
      CREATE REQUIRED PROPERTY with_reposts -> std::bool {
          SET default := false;
      };
  };
};
