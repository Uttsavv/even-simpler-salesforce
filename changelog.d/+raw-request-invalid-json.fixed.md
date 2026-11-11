If `sf.request()` gets a 2xx `application/json` body that is not valid JSON, the `sf.request` event now reports `outcome="error"`. Previously the event said `ok` and the error was raised afterwards.
