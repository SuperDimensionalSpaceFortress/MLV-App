---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/smb-listing-that-opens-files-feeds-the-av-scanner
rule: Never list a large SMB directory with a tool that stats every entry (Git for Windows `ls -t`, `ls -l`, `find -newer`); enumerate names and times from the directory listing (.NET `EnumerateFileSystemInfos`), keep the newest N, and rotate mailbox directories into dated archive folders.
mechanism: The MSYS coreutils stat opens each file to read its times, and real-time antivirus scans every open, file by file over the network. A directory enumeration returns the same metadata without opening anything. A process-name exclusion for git.exe does not cover usr\bin\ls.exe. Measured 2026-10-17: `ls -t` of a 20.7k-file job mailbox ran over 10 minutes at about 48 s of scan time per minute; the enumeration listed it in about 2.5 s.
check: pwsh -NoProfile -File tools/testing/test-agent-share-rotation.ps1 (MLV-App; bounded lister plus whole-job rotation)
supersedes: none
evidence: measured
