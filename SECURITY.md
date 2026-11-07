# Security policy

Report suspected vulnerabilities privately through GitHub repository security
advisories. Include a minimal reproduction and the affected SDK version. Keep
signing credentials, model connection details and real target data out of reports.

Deploy only publisher-reviewed callable implementations under an unprivileged
identity. A registered function executes trusted Python with the worker's
authority. Restrict journal file access with operating-system ACLs and protect
the controller signing key separately from the endpoint trust store. Treat
machine outputs as untrusted data and use explicit result projection.

The in-process worker uses cooperative deadlines for callables. A production
deployment needs a reviewed isolation adapter that quiesces work before
acknowledging cancellation or a timeout.
