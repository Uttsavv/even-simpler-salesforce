from simple_salesforce import Salesforce

# esf-migrate: TODO[login] construct esf.Salesforce / esf.AsyncSalesforce with a login flow: ClientCredentials, JwtBearer or WebServerFlow; StaticToken(access_token, instance_url) replaces session_id=/instance=; username/password login isn't supported
sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")

sf.request("GET", "sobjects/Lead/describe")
sf.request("GET", "limits", params={"a": "1"})
sf.request("POST", "sobjects/Lead", json={"LastName": "Doe"})
# esf-migrate: TODO[restful] use sf.request(method, path, params=..., json=...)
sf.restful("sobjects/Lead", method="POST", data='{"x": 1}')
sf.request("POST", "/services/apexrest/orders/submit", json={"orderId": "1"})
sf.request("GET", "/services/apexrest/orders/status")
# esf-migrate: TODO[apexecute] use sf.request(METHOD, "/services/apexrest/<path>", json=data)
sf.apexecute(path_variable, method="GET")
