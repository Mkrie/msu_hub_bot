CREATE MIGRATION m1dqhwhdybu23gdi4646fybboc5qhwy6xk6g5mmfvdfrc4qwbh2xva
    ONTO m13wnrryyuziryv3hu7j6czta4g5kj6s7k5g6v2ud4e3si63wdpkva
{
  ALTER TYPE meta::HasMetadata {
      ALTER PROPERTY metadata {
          SET default := (<std::json>'{}');
      };
  };
};
