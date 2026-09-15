CREATE MIGRATION m14tsw427amdvfqy4dgat22qr67u4zru4yrbdeeo6djozg467722la
    ONTO m16ltq3mvtz3gtxazgfvlxgoc3ad5pjm2eipmsiou2cexzp32spqoq
{
  ALTER TYPE msu_hub::EcosystemChat {
      ALTER PROPERTY links_message_id {
          RENAME TO pinned_message_id;
      };
  };
};
