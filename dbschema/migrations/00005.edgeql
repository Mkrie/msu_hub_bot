CREATE MIGRATION m1ib3ut4eep4coernbxsqvoxnrq5oxyub3armvifxn4tyelk54sxaq
    ONTO m1lgg7vte3rlhwp62uuw52y6hvo2zp6uuvmz4gblb6krmgyfgq46ga
{
  ALTER TYPE telegram::Chat {
      CREATE PROPERTY full_name := ((.title IF EXISTS (.title) ELSE (((.first_name ++ ' ') ++ .last_name) IF EXISTS (.last_name) ELSE .first_name)));
  };
};
