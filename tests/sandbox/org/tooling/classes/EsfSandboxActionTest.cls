// esf-sandbox: Apex test class for runTestsAsynchronous (V46) and quick deploy (V86).
@IsTest
private class EsfSandboxActionTest {
    @IsTest
    static void greets() {
        EsfSandboxAction.Request r = new EsfSandboxAction.Request();
        r.name = 'esf';
        List<EsfSandboxAction.Result> out = EsfSandboxAction.run(new List<EsfSandboxAction.Request>{ r });
        System.assertEquals('hello esf', out[0].greeting);
    }
}
