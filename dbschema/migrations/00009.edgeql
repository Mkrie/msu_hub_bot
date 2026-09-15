CREATE MIGRATION m13wnrryyuziryv3hu7j6czta4g5kj6s7k5g6v2ud4e3si63wdpkva
    ONTO m1g6vdw2tuectkruptblcse77upmxevcdng5myhwnth4w4dacwudzq
{
  ALTER TYPE meta::HasMetadata {
      ALTER PROPERTY metadata {
          SET default := (<std::json>{});
      };
  };
};
