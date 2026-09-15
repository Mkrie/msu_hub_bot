CREATE MIGRATION m1lgg7vte3rlhwp62uuw52y6hvo2zp6uuvmz4gblb6krmgyfgq46ga
    ONTO m1uwql657kosknc2q3pvvrmfcagsblsf7rr7a35lxyokjp7jl47mpq
{
  ALTER TYPE vk_tg::VkWallPosting {
      CREATE CONSTRAINT std::exclusive ON ((.owner_id, .chat_id));
      ALTER PROPERTY chat_id {
          DROP CONSTRAINT std::exclusive;
      };
      ALTER PROPERTY owner_id {
          DROP CONSTRAINT std::exclusive;
      };
  };
};
