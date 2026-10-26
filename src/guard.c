#include "support.h"

static void usage(void) {
    fputs("headroom --state NEW_DIRECTORY --soft-mib N --hard-mib N "
          "[--allow-stop] [--grace-ms N] [--timeout-ms N] -- EXECUTABLE [ARG ...]\n"
          "Limits are group committed memory, not physical RAM. "
          "Only newly launched processes are managed.\n", stderr);
}

/* Failure can occur after process creation but before job assignment. Closing
   a process handle never terminates such a suspended, uncontained child. */
static int cleanup_launch(HANDLE group, HANDLE process, int assigned) {
    if (!process) return 1;
    if (!assigned) {
        if (WaitForSingleObject(process, 0) == WAIT_OBJECT_0) return 1;
        if (!TerminateProcess(process, 70)) return 0;
        return WaitForSingleObject(process, 5000) == WAIT_OBJECT_0;
    }
    if (!TerminateJobObject(group, 70)) return 0;
    ULONGLONG deadline = GetTickCount64() + 5000;
    for (;;) {
        JOBOBJECT_BASIC_ACCOUNTING_INFORMATION accounting = {0};
        if (!QueryInformationJobObject(group, JobObjectBasicAccountingInformation,
                                       &accounting, sizeof(accounting), NULL)) return 0;
        if (!accounting.ActiveProcesses) return 1;
        if (GetTickCount64() >= deadline) return 0;
        Sleep(10);
    }
}

int wmain(int argc, wchar_t **argv) {
    ULONGLONG soft = 0, hard = 0, grace = 2000, timeout = 0;
    const wchar_t *state_arg = NULL;
    int allow_stop = 0, cmd = 0, result = 70, failure = 1;
    int assigned = 0, handler_registered = 0;
    HANDLE group = NULL, port = NULL;
    PROCESS_INFORMATION pi = {0};
    STARTUPINFOEXW startup = {0};
    int attributes_initialized = 0;
    Journal journal = {INVALID_HANDLE_VALUE, GetTickCount64(), ""};
    wchar_t state[PATH_CAP], path[PATH_CAP], executable[PATH_CAP];
    wchar_t *line = NULL;
    for (int i = 1; i < argc; ++i) {
        if (!wcscmp(argv[i], L"--")) { cmd = i + 1; break; }
        if (!wcscmp(argv[i], L"--allow-stop")) { allow_stop = 1; continue; }
        if (i + 1 >= argc) { usage(); return 64; }
        if (!wcscmp(argv[i], L"--state")) state_arg = argv[++i];
        else if (!wcscmp(argv[i], L"--soft-mib")) {
            if (!number(argv[++i], SIZE_MAX / MIB, &soft)) { usage(); return 64; }
        } else if (!wcscmp(argv[i], L"--hard-mib")) {
            if (!number(argv[++i], SIZE_MAX / MIB, &hard)) { usage(); return 64; }
        } else if (!wcscmp(argv[i], L"--grace-ms")) {
            if (!number(argv[++i], 60000, &grace)) { usage(); return 64; }
        } else if (!wcscmp(argv[i], L"--timeout-ms")) {
            if (!number(argv[++i], 86400000, &timeout)) { usage(); return 64; }
        } else { usage(); return 64; }
    }
    if (!state_arg || !soft || hard <= soft || !grace || !cmd || cmd >= argc) {
        usage(); return 64;
    }
    soft *= MIB;
    hard *= MIB;
    DWORD length = GetFullPathNameW(state_arg, PATH_CAP, state, NULL);
    if (!length || length >= PATH_CAP || !CreateDirectoryW(state, NULL)) {
        fputs("State must be a new directory under an existing parent. Nothing was launched.\n", stderr);
        return 73;
    }
    GUID guid;
    HRESULT guid_result = CoCreateGuid(&guid);
    if (FAILED(guid_result)) {
        SetLastError(HRESULT_FACILITY(guid_result) == FACILITY_WIN32
            ? (DWORD)HRESULT_CODE(guid_result) : ERROR_GEN_FAILURE);
        goto cleanup;
    }
    snprintf(journal.nonce, sizeof(journal.nonce),
        "%08lx-%04x-%04x-%02x%02x-%02x%02x%02x%02x%02x%02x",
        (unsigned long)guid.Data1, guid.Data2, guid.Data3,
        guid.Data4[0], guid.Data4[1], guid.Data4[2], guid.Data4[3],
        guid.Data4[4], guid.Data4[5], guid.Data4[6], guid.Data4[7]);
    if (!join_path(path, state, L"events.jsonl")) goto cleanup;
    journal.log = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL,
        CREATE_NEW, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_WRITE_THROUGH, NULL);
    if (journal.log == INVALID_HANDLE_VALUE) goto cleanup;
    if (!event(&journal, "prepared", 0, 0, 0)) goto cleanup;
    group = CreateJobObjectW(NULL, NULL);
    if (!group) goto cleanup;
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = {0};
    limits.BasicLimitInformation.LimitFlags =
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_JOB_MEMORY;
    limits.JobMemoryLimit = (SIZE_T)hard;
    if (!SetInformationJobObject(group, JobObjectExtendedLimitInformation,
                                 &limits, sizeof(limits))) goto cleanup;
    port = CreateIoCompletionPort(INVALID_HANDLE_VALUE, NULL, 0, 1);
    if (!port) goto cleanup;
    JOBOBJECT_ASSOCIATE_COMPLETION_PORT association = {(PVOID)1, port};
    if (!SetInformationJobObject(group, JobObjectAssociateCompletionPortInformation,
                                 &association, sizeof(association))) goto cleanup;
    JOBOBJECT_NOTIFICATION_LIMIT_INFORMATION notification = {0};
    notification.LimitFlags = JOB_OBJECT_LIMIT_JOB_MEMORY;
    notification.JobMemoryLimit = soft;
    if (!SetInformationJobObject(group, JobObjectNotificationLimitInformation,
                                 &notification, sizeof(notification))) goto cleanup;
    /* Environment changes are confined to this guardian process and its child. */
    if (!SetEnvironmentVariableW(L"HEADROOM_CONTROL_DIR", state)) goto cleanup;
    DWORD found = SearchPathW(NULL, argv[cmd], L".exe", PATH_CAP, executable, NULL);
    if (!found || found >= PATH_CAP) goto cleanup;
    line = calloc(CMD_CAP, sizeof(wchar_t));
    if (!line) { SetLastError(ERROR_NOT_ENOUGH_MEMORY); goto cleanup; }
    if (!command_line(line, argc - cmd, &argv[cmd])) goto cleanup;
    InterlockedExchange(&interrupted, 0);
    if (!SetConsoleCtrlHandler(control, TRUE)) goto cleanup;
    handler_registered = 1;
    startup.StartupInfo.cb = sizeof(startup);
    SIZE_T attribute_size = 0;
    InitializeProcThreadAttributeList(NULL, 1, 0, &attribute_size);
    if (!attribute_size) goto cleanup;
    startup.lpAttributeList = calloc(1, attribute_size);
    if (!startup.lpAttributeList) { SetLastError(ERROR_NOT_ENOUGH_MEMORY); goto cleanup; }
    if (!InitializeProcThreadAttributeList(startup.lpAttributeList, 1, 0, &attribute_size)) goto cleanup;
    attributes_initialized = 1;
    if (!UpdateProcThreadAttribute(startup.lpAttributeList, 0,
        PROC_THREAD_ATTRIBUTE_JOB_LIST, &group, sizeof(group), NULL, NULL)) goto cleanup;
    /* No create/assign crash gap: the kernel binds ownership while creating
       the still-suspended child. Unsupported/restricted assignment fails closed. */
    if (!CreateProcessW(executable, line, NULL, NULL, FALSE,
        CREATE_SUSPENDED | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT,
        NULL, NULL, &startup.StartupInfo, &pi)) goto cleanup;
    assigned = 1;
    if (!event(&journal, "contained", 0, 1, pi.dwProcessId)) goto cleanup;
    if (!InterlockedCompareExchange(&interrupted, 0, 0)
        && ResumeThread(pi.hThread) == (DWORD)-1) goto cleanup;
    CloseHandle(pi.hThread);
    pi.hThread = NULL;
    ULONGLONG stop_deadline = 0, kill_deadline = 0, next_sample = 0;
    ULONGLONG last_commit = 0;
    int stopping = 0, killed = 0, ack = 0, warned = 0, timed_out = 0;
    int interrupted_stop = 0;
    for (;;) {
        ULONGLONG now = GetTickCount64();
        JOBOBJECT_BASIC_ACCOUNTING_INFORMATION accounting = {0};
        if (!QueryInformationJobObject(group, JobObjectBasicAccountingInformation,
                                       &accounting, sizeof(accounting), NULL)) goto cleanup;
        now = GetTickCount64();
        /* Pending cancellation/deadline cannot be overwritten by an empty
           group observed in this same cycle. Interrupt takes precedence. */
        if (InterlockedCompareExchange(&interrupted, 0, 0) && !interrupted_stop) {
            if (!event(&journal, "interrupt_intent", last_commit, accounting.ActiveProcesses, 130)) goto cleanup;
            if (accounting.ActiveProcesses && !TerminateJobObject(group, 130)) goto cleanup;
            interrupted_stop = killed = 1;
            kill_deadline = now + 5000;
        }
        if (timeout && now - journal.started >= timeout && !killed) {
            if (!event(&journal, "timeout_intent", last_commit, accounting.ActiveProcesses, 124)) goto cleanup;
            if (accounting.ActiveProcesses && !TerminateJobObject(group, 124)) goto cleanup;
            timed_out = killed = 1;
            kill_deadline = now + 5000;
        }
        if (!accounting.ActiveProcesses) {
            DWORD code;
            if (!GetExitCodeProcess(pi.hProcess, &code)) goto cleanup;
            if (stopping && !ack && acknowledged(state, &journal)) {
                ack = 1;
                if (!event(&journal, "checkpoint_acknowledged", last_commit, 0, 0)) goto cleanup;
            }
            /* The bounded ack worker can still be reading a flushed ack after
             * the writer exits. Collect it within the ORIGINAL grace window,
             * just as the fleet does, rather than finalize on one 5 ms miss.
             * An interrupt/deadline must not acquire a new acknowledgment wait. */
            if (stopping && !ack && !killed && now < stop_deadline
                && !InterlockedCompareExchange(&interrupted, 0, 0)
                && (!timeout || now - journal.started < timeout)) {
                Sleep(10);
                continue;
            }
            result = interrupted_stop ? 130 : timed_out ? 124 : stopping ? 125 : (int)code;
            if (!event(&journal, interrupted_stop ? "interrupted" : timed_out ? "timeout" : stopping ? "stopped" : "completed",
                       last_commit, 0, code)) { result = 70; }
            else failure = 0;
            break;
        }
        if (stopping && !ack && acknowledged(state, &journal)) {
            ack = 1;
            if (!event(&journal, "checkpoint_acknowledged", last_commit,
                       accounting.ActiveProcesses, 0)) goto cleanup;
        }
        now = GetTickCount64();
        if (stopping && now >= stop_deadline && !killed) {
            if (!event(&journal, "terminate_intent", last_commit, accounting.ActiveProcesses, 125)) goto cleanup;
            if (!TerminateJobObject(group, 125)) goto cleanup;
            killed = 1;
            kill_deadline = now + 5000;
        }
        if (killed && now >= kill_deadline) {
            if (!event(&journal, "cleanup_unconfirmed", last_commit, accounting.ActiveProcesses, 70)) goto cleanup;
            goto cleanup;
        }
        /* A 1 Hz heartbeat is evidence of liveness, not an RSS measurement. */
        if (now >= next_sample) {
            if (!event(&journal, "heartbeat", last_commit, accounting.ActiveProcesses, 0)) goto cleanup;
            next_sample = now + 1000;
        }
        DWORD message = 0;
        ULONG_PTR key = 0;
        LPOVERLAPPED process = NULL;
        DWORD wait_ms = stopping || killed ? 100 : 1000;
        if (stopping && !killed && stop_deadline > now && stop_deadline - now < wait_ms)
            wait_ms = (DWORD)(stop_deadline - now);
        if (timeout && now - journal.started < timeout) {
            ULONGLONG left = timeout - (now - journal.started);
            if (left < wait_ms) wait_ms = (DWORD)left;
        }
        BOOL received = GetQueuedCompletionStatus(port, &message, &key, &process, wait_ms);
        if (!received && GetLastError() != WAIT_TIMEOUT) goto cleanup;
        if (received && key == 1 && message == JOB_OBJECT_MSG_JOB_MEMORY_LIMIT) {
            if (!event(&journal, "allocation_denied", last_commit, accounting.ActiveProcesses, 0)) goto cleanup;
        }
        if (received && key == 1 && message == JOB_OBJECT_MSG_NOTIFICATION_LIMIT && !warned && !killed) {
            JOBOBJECT_LIMIT_VIOLATION_INFORMATION violation = {0};
            if (!QueryInformationJobObject(group, JobObjectLimitViolationInformation,
                                           &violation, sizeof(violation), NULL)) goto cleanup;
            if (!(violation.ViolationLimitFlags & JOB_OBJECT_LIMIT_JOB_MEMORY)
                || violation.JobMemory <= soft) continue;
            last_commit = violation.JobMemory;
            warned = 1;
            if (!event(&journal, "soft_limit_observed", last_commit, accounting.ActiveProcesses, 0)) goto cleanup;
            if (!allow_stop) continue;
            /* The task may finish while its memory notification is queued. */
            if (!QueryInformationJobObject(group, JobObjectBasicAccountingInformation,
                                           &accounting, sizeof(accounting), NULL)) goto cleanup;
            if (!accounting.ActiveProcesses) continue;
            if (!event(&journal, "checkpoint_intent", last_commit, accounting.ActiveProcesses, 125)
                || !request_stop(state, &journal)) goto cleanup;
            stopping = 1;
            stop_deadline = GetTickCount64() + grace;
        }
    }
cleanup:
    if (failure) {
        DWORD error = GetLastError();
        if (!error) error = ERROR_GEN_FAILURE;
        if (!cleanup_launch(group, pi.hProcess, assigned))
            fputs("Owned launch cleanup could not be confirmed; job close will retry containment.\n", stderr);
        if (journal.log != INVALID_HANDLE_VALUE) event(&journal, "guardian_error", 0, 0, error);
        fprintf(stderr, "Guardian failed (Windows error %lu). Owned children are closed, never detached.\n",
                (unsigned long)error);
    }
    if (handler_registered) SetConsoleCtrlHandler(control, FALSE);
    if (attributes_initialized) DeleteProcThreadAttributeList(startup.lpAttributeList);
    free(startup.lpAttributeList);
    if (group) CloseHandle(group);
    if (pi.hThread) CloseHandle(pi.hThread);
    if (pi.hProcess) CloseHandle(pi.hProcess);
    if (port) CloseHandle(port);
    if (journal.log != INVALID_HANDLE_VALUE) CloseHandle(journal.log);
    free(line);
    return result;
}
