// esf-sandbox: Apex REST probe (V34). GET ?mode=throw raises an unhandled exception.
@RestResource(urlMapping='/esf-sandbox/*')
global with sharing class EsfSandboxRest {
    global class Reply {
        public Boolean ok = true;
        public String method;
        public String name;
        public Map<String, String> params;
    }

    @HttpGet
    global static Reply doGet() {
        RestRequest req = RestContext.request;
        if (req.params.get('mode') == 'throw') {
            Integer zero = 0;
            Integer boom = 1 / zero;
        }
        Reply r = new Reply();
        r.method = 'GET';
        r.params = req.params;
        return r;
    }

    @HttpPost
    global static Reply doPost(String name) {
        Reply r = new Reply();
        r.method = 'POST';
        r.name = name;
        return r;
    }
}
