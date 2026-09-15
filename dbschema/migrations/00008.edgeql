CREATE MIGRATION m1g6vdw2tuectkruptblcse77upmxevcdng5myhwnth4w4dacwudzq
    ONTO m14tsw427amdvfqy4dgat22qr67u4zru4yrbdeeo6djozg467722la
{
  ALTER TYPE meta::HasCreated {
      CREATE INDEX ON (.created);
  };
};
