from simple_salesforce import Salesforce

sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")

sf.restful("sobjects/Lead/describe")
sf.restful("limits", method="GET", params={"a": "1"})
sf.restful("sobjects/Lead", method="POST", json={"LastName": "Doe"})
sf.restful("sobjects/Lead", method="POST", data='{"x": 1}')
sf.apexecute("orders/submit", method="POST", data={"orderId": "1"})
sf.apexecute("/orders/status")
sf.apexecute(path_variable, method="GET")
