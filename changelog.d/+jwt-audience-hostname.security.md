JwtBearer infers the sandbox audience from the parsed hostname (`*.sandbox.my.salesforce.com`), not a substring match, so a look-alike domain can't select the sandbox audience.
