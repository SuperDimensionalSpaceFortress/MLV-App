---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/wmi-create-child-inherits-gpfault-error-box
rule: route every detached launch that goes through a WMI process-create call (Win32_Process Create, Invoke-CimMethod, Invoke-WmiMethod) through ONE launcher that sets SEM_NOGPFAULTERRORBOX (0x2) before the real child starts: it creates a stub that ORs 0x8003 into its own error mode, starts the unchanged command line with one CreateProcess that does not pass CREATE_DEFAULT_ERROR_MODE, waits, returns the exit code, so it and all it starts inherit 0x8003. Never call the WMI create directly from a script, and never rely on the child to set its own mode (a native crash can come first). Outside this remedy: a hop that passes CREATE_DEFAULT_ERROR_MODE, a child that resets its own mode.
mechanism: a WMI-created process is not a child of the caller: its parent is the WMI provider host and it starts at error mode 0x8001, no 0x2, so a crash raises the modal "Application Error" box on the interactive desktop. Seen 2026-11-13 17:21:15 CST: console_tests.exe, Qt6Core.dll, 0xc0000005.
applies: mlv-app
check: py -3 -m pytest tools/coordination/test_coordination_guardrails.py -k "error_mode or wmi_create". The four error_mode tests cover Invoke-Lane's own block, not a WMI child: two read 0x8003 in a grandchild (Windows with pwsh), the other two and both wmi_create tests skip off a pwsh host. The clause "no direct WMI create" is test_tracked_powershell_has_no_direct_wmi_create_outside_the_allow_list over tracked *.ps1/*.psm1 in any letter case (`git ls-files -z -- ':(icase)*.ps1' ':(icase)*.psm1'`, test_coordination_guardrails.py:1713), RED-proved by test_wmi_create_scan_refuses_each_direct_create_spelling; untracked lane scripts are guarded board-side. Stub half measured 2026-11-14 (0x8001 plain, 0x8003 stub): board-local, not pushed.
supersedes: none
evidence: measured
