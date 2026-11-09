from simple_salesforce import Salesforce

sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")


@register(sf.Lead.create({"LastName": "Doe"}))
def handler(default=sf.Contact.describe()):
    pass


class Model(Base, meta=sf.Account.metadata()):
    pass


match sf.Lead.create({"LastName": "Doe"}):
    case _:
        pass
