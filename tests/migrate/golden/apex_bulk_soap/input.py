from simple_salesforce import Salesforce


class Repo:
    def __init__(self, sf_client: Salesforce) -> None:
        self.sf_client = sf_client

    def consent(self, path, data):
        return self.sf_client.apexecute(path, method="POST", data=data)

    def otp(self):
        return self.sf_client.apexecute("api/v1/pilot-otp/send", method="post", data={"a": 1})

    def ping(self):
        return self.sf_client.apexecute("/api/v1/ping")

    def odd(self, verb):
        a = self.sf_client.apexecute("api/v1/x", method=verb)
        b = self.sf_client.apexecute("api/v1/x", method="GET", data={"q": 1})
        return a, b, self.sf_client.apexecute("api/v1/x?y=1")

    def load(self, payloads):
        return self.sf_client.bulk.Lead.insert(payloads, batch_size=10000, use_serial=False)

    def soap(self):
        url = f"https://{self.sf_client.sf_instance}/services/Soap/u/60.0"
        return url, self.sf_client.session_id


def top(sf: Salesforce, rows, soql):
    sf.bulk.Contact.upsert(rows, external_id_field="Ext__c")
    sf.bulk.Lead.hard_delete(rows)
    found = sf.bulk.Account.query_all(soql)
    jobs = sf.bulk
    return found, jobs, sf.toolingexecute("query/?q=SELECT+Id+FROM+ApexClass")


def more(sf: Salesforce, payloads, flag):
    sf.bulk.Lead.insert(data=payloads, batch_size=500)
    sf.bulk.Lead.insert(load(token="abc123secret"))
    return sf.apexecute("a" if flag else "b")


def guarded(sf: Salesforce):
    from simple_salesforce import SalesforceResourceNotFound

    try:
        return sf.apexecute("api/v1/maybe", method="GET")
    except SalesforceResourceNotFound:
        return None


def aliased(sf: Salesforce):
    from simple_salesforce import SalesforceResourceNotFound as Missing

    try:
        return sf.apexecute("api/v1/other", method="GET")
    except (ValueError, Missing):
        return None


def other_module(sf: Salesforce, other):
    from simple_salesforce import SalesforceResourceNotFound as Missing  # noqa: F401

    try:
        return sf.apexecute("api/v1/third", method="GET")
    except other.Missing:
        return None
