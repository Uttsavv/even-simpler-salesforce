`sf.request` events report `outcome="error"` when a 2xx body that should be JSON is not; previously the event said `ok` and the error was raised afterwards.
