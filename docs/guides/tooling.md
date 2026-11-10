# Tooling API

`sf.tooling` calls the Tooling API under `/services/data/vXX.X/tooling/`. Use it to read and
change Apex classes, triggers and debug settings, to run anonymous Apex and to start Apex
tests.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)

result = await sf.tooling.execute_anonymous("System.debug('hello');", capture_log=True)
print(result.success)  # True
print(result.log)  # the debug log, with '...|USER_DEBUG|[1]|DEBUG|hello'
```

## Query Tooling objects

`query()` and `query_iter()` work like `sf.query()` and `sf.query_iter()`, for Tooling objects
such as `ApexClass`, `ApexTrigger`, `TraceFlag` or `ApexLog`. `{}` placeholders are filled and
quoted in the same way.

```python
page = await sf.tooling.query("SELECT Id, Name FROM ApexClass WHERE Name = {}", "OrderService")
print(page.records[0]["Name"])  # 'OrderService'

async for cls in sf.tooling.query_iter("SELECT Id, Name, Status FROM ApexClass"):
    print(cls["Name"])
```

To get the next page of `query()`, pass its `cursor` to `sf.query_more()`. The client reads the
next page from the Tooling API. `query()` doesn't take `page_size`, `include_deleted` or
`expand_children`.

## Read and change Tooling records

`sobject(name)` has the methods of `sf.sobject(name)`: `get`, `get_by_external_id`, `create`,
`update`, `upsert`, `delete`, `describe` and the others. The paths start with `tooling/`. The
describe cache keeps Tooling and REST describes apart.

```python
classes = sf.tooling.sobject("ApexClass")
cls = await classes.get(page.records[0]["Id"])
print(cls["Status"])  # 'Active'

level_id = await sf.tooling.sobject("DebugLevel").create(
    {"DeveloperName": "Verbose", "MasterLabel": "Verbose", "ApexCode": "FINEST"}
)
await sf.tooling.sobject("DebugLevel").delete(level_id)
```

Event names and `sf.usage` show these calls as `tooling.sobject.get`, `tooling.sobject.create`
and so on.

## Run anonymous Apex

`execute_anonymous(code)` compiles and runs Apex in one call. It returns an
`esf.AnonymousResult`. Anonymous Apex can't return values. To see output, write it with
`System.debug` and pass `capture_log=True` (see below).

```python
result = await sf.tooling.execute_anonymous("System debug('x');")
print(result.compiled)  # False
print(result.compile_problem, result.line, result.column)  # Unexpected token 'debug'. 1 8

result = await sf.tooling.execute_anonymous("Integer i = 1 / 0;")
print(result.compiled, result.success)  # True False
print(result.exception_message)  # 'System.MathException: Divide by 0'
```

| Attribute | Meaning |
|---|---|
| `compiled` | the code compiled |
| `success` | the code compiled and ran without an unhandled exception |
| `compile_problem` | why it didn't compile, else `None` |
| `exception_message`, `exception_stack_trace` | the unhandled exception, else `None` |
| `line`, `column` | where the problem or exception is, else `None` |
| `log` | the debug log, with `capture_log=True` only |

A compile problem or an exception is a normal result, not an error. The call raises only when
the request itself fails.

The code runs as the connected user, and that user needs the "Author Apex" permission. The code
goes in the URL, so its length is limited. If Salesforce rejects a long URL (HTTP 414), the call
raises `RequestTooLarge` and the code didn't run. Put long code in an Apex class instead.

!!! warning "Never resent"
    Anonymous Apex can change data. The client never calls `execute_anonymous` on its own, and
    it never resends the call after it may have reached Salesforce, also after a timeout or a
    5xx. If you get `RequestTimeout`, the code may have run. Check before you call it again.

## Capture the debug log

With `capture_log=True` the client returns the debug log of the run in `result.log`:

1. It checks whether the user already has an active `USER_DEBUG` trace flag.
2. If not, it creates a `TraceFlag` for the user that expires after 10 minutes. The flag uses a
   `DebugLevel` named `esf_anonymous_apex`, which the client creates once and then reuses.
3. It runs the code.
4. It finds the new `ApexLog` and reads its body.
5. It deletes its trace flag, also when the run fails.

```python
result = await sf.tooling.execute_anonymous(
    "System.debug('step 1');\nSystem.debug('step 2');", capture_log=True
)
debug_lines = [line for line in result.log.splitlines() if "|USER_DEBUG|" in line]
print(len(debug_lines))  # 2
```

This costs up to seven more API calls: four queries and creates before the run, and the log
query, the log body and the delete after it. With `esf.StaticToken`, one more call to
`userinfo` finds the user id. They all count in `sf.usage`. If the user
already has an active `USER_DEBUG` flag, the client uses it and doesn't delete it.

If the client can't read the log, `result.log` is `None` and a `RuntimeWarning` says why. The
client doesn't raise then, because the code already ran. If it can't delete its trace flag, it
warns too. The flag then expires on its own within 10 minutes.

!!! note "Limitation"
    Salesforce doesn't link a log to the request that made it. The client takes the newest log
    of the user whose operation is `executeAnonymous` and that started after the run began
    (allowing 60 seconds of clock difference). If the same user runs anonymous Apex from
    another session at the same moment, the client can return that log instead. Use a
    dedicated integration user if this matters.

## Run Apex tests

`run_tests_async(class_ids)` queues the tests of Apex test classes and returns the
`AsyncApexJob` id. It doesn't wait for the tests to finish.

```python
test_class = (await sf.tooling.query("SELECT Id FROM ApexClass WHERE Name = {}", "OrderServiceTest")).records[
    0
]
job_id = await sf.tooling.run_tests_async([test_class["Id"]])
print(job_id)  # '707...'
```

To get the outcome, query `ApexTestResult` with `AsyncApexJobId = {job_id}` until every test
has a result.

## Code completions

`completions(type)` returns the symbols that editors offer for code completion. `type` is
`"apex"` (system classes and their methods) or `"visualforce"` (components).

```python
symbols = await sf.tooling.completions("apex")
print("Math" in symbols["publicDeclarations"]["System"])  # True
```

## Methods

| Method | Returns | Salesforce call |
|---|---|---|
| `query(soql, *values, model=)` | `QueryResult` | `GET tooling/query?q=` |
| `query_iter(soql, *values, model=)` | async iterator of rows | same, then `nextRecordsUrl` |
| `sobject(name)` | the `sf.sobject(name)` methods | `tooling/sobjects/{name}/...` |
| `execute_anonymous(code, *, capture_log=False)` | `AnonymousResult` | `GET tooling/executeAnonymous/?anonymousBody=` |
| `run_tests_async(class_ids)` | `AsyncApexJob` id | `POST tooling/runTestsAsynchronous/` |
| `completions(type)` | `dict` | `GET tooling/completions?type=` |

## Test code that uses the Tooling API

The fakes in `esf.testing` have `tooling` too. `fake.on_execute_anonymous(match, result)`
scripts the answer to `execute_anonymous`. The fake never runs Apex. `tooling.query` answers from
`fake.on_query` scripts, and `tooling.sobject(name)` uses the fake's record store.

```python
from even_simpler_salesforce.testing import FakeSalesforce


def reset_scores(sf) -> bool:  # code under test
    return sf.tooling.execute_anonymous("LeadScoring.resetAll();").success


def test_reset_scores(esf_fake: FakeSalesforce) -> None:
    esf_fake.on_execute_anonymous("LeadScoring.resetAll();")  # compiled and successful
    assert reset_scores(esf_fake)

    esf_fake.on_execute_anonymous(
        "LeadScoring.resetAll();",
        esf.AnonymousResult(compiled=True, success=False, exception_message="System.LimitException"),
    )
    assert not reset_scores(esf_fake)
    assert esf_fake.calls_for("tooling.execute_anonymous")[0].args["code"] == "LeadScoring.resetAll();"
```

`match` is the code, a compiled regex or a predicate. `result` is an `AnonymousResult`, a
callable that gets the code, or an exception to raise. The result's `log` is returned only with
`capture_log=True`. Code that no script matches raises `UnscriptedCall`.
`fake.set_completions(type, value)` sets what `completions()` returns, and `run_tests_async()`
returns a new job id.
