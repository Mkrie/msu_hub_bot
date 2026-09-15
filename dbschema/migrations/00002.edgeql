CREATE MIGRATION m1j2gzqy6k3vpucseyofs2ks2r2m7v33bnd7u52htgke3b74nkmhia
    ONTO m1ltyxg4huggzrfgbvj2k653woquxjqfm675uwpdonlfyw4b3b44zq
{
  ALTER TYPE meta::HasMetadata {
      ALTER PROPERTY metadata {
          SET default := (<std::json>'{}');
      };
  };
};
