import even_simpler_salesforce

esf = "estimated ship date"


def get(sf):
    try:
        return sf.query("SELECT Id FROM Lead")
    except even_simpler_salesforce.NotFound:
        return esf
