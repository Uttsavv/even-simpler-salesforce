// esf-sandbox: invocable action probe (V31-V33). name = 'fail' throws, failing the call.
public with sharing class EsfSandboxAction {
    public class Request {
        @InvocableVariable(label='Name' required=true)
        public String name;
    }

    public class Result {
        @InvocableVariable(label='Greeting')
        public String greeting;
    }

    @InvocableMethod(label='ESF Sandbox Echo' description='esf-sandbox probe')
    public static List<Result> run(List<Request> requests) {
        List<Result> out = new List<Result>();
        for (Request r : requests) {
            if (r.name == 'fail') {
                throw new IllegalArgumentException('esf-sandbox: action failure probe');
            }
            Result x = new Result();
            x.greeting = 'hello ' + r.name;
            out.add(x);
        }
        return out;
    }
}
