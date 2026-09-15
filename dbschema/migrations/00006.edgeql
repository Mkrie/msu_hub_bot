CREATE MIGRATION m16ltq3mvtz3gtxazgfvlxgoc3ad5pjm2eipmsiou2cexzp32spqoq
    ONTO m1ib3ut4eep4coernbxsqvoxnrq5oxyub3armvifxn4tyelk54sxaq
{
  ALTER TYPE msu_hub::EcosystemChat {
      ALTER PROPERTY is_hidden {
          SET REQUIRED USING (false);
      };
  };
  ALTER TYPE msu_hub::EcosystemChat {
      ALTER PROPERTY name {
          SET REQUIRED USING ('');
      };
  };
  ALTER TYPE msu_hub::EcosystemChat {
      ALTER PROPERTY section {
          SET REQUIRED USING ('other');
      };
  };
};
