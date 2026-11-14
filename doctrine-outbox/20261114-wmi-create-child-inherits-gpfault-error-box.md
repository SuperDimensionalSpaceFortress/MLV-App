---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/wmi-create-child-inherits-gpfault-error-box
rule: route every detached launch that goes through a WMI process-create call (Win32_Process Create, Invoke-CimMethod, Invoke-WmiMethod) through ONE launcher that sets SEM_NOGPFAULTERRORBOX (0x2) before the real child starts: the launcher creates a small stub, the stub ORs 0x8003 into its own error mode, starts the unchanged command line with one CreateProcess, waits and returns the exit code, so the command line and everything it starts inherit 0x8003. Do not call the WMI create directly from a lane script, and do not rely on the child to set its own mode (a native crash can come before any of its code runs).
mechanism: a WMI-created process is not a child of the caller; its measured ancestor chain is the command shell, the WMI provider host, then the service host and the service manager, and it starts at error mode 0x8001 (SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX, no 0x2). A crash in it raises the modal "Application Error" box on the interactive desktop, which nobody dismisses on an unattended host. Seen 2026-11-13 17:21:15 CST: console_tests.exe, module Qt6Core.dll, 0xc0000005, Application log event 1000, dialog left up. The Qt crash itself is one occurrence in 14 days and was not chased.
applies: mlv-app
check: measured 2026-11-14, no crash provoked: a probe child and its own child read GetErrorMode 0x8001 through the plain WMI launcher (RED) and 0x8003 through the stub launcher (GREEN); the hidden-detached launcher's Pester test, check 10, asserts both read 0x8003 (RED 20/21 against the old launcher, GREEN 21/21), a board-local harness not yet pushed.
supersedes: none
evidence: measured
