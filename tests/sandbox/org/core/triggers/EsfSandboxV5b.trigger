// esf-sandbox: fails on purpose for verify_sandbox.py V5b (Company = ESF_ADDERROR / ESF_THROW).
trigger EsfSandboxV5b on Lead (before insert) {
    for (Lead l : Trigger.new) {
        if (l.Company == 'ESF_ADDERROR') {
            l.addError('esf-sandbox: addError probe');
        } else if (l.Company == 'ESF_THROW') {
            Integer zero = 0;
            Integer boom = 1 / zero; // unhandled MathException
        }
    }
}
