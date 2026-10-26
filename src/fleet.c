#include "support.h"
#include "policy.h"
#include "output.h"
#include "intake.h"

#define MAX_JOBS 64
#define MAX_PLAN (16U * 1024U * 1024U)
#define MAX_BYTES (1048576ULL * MIB)

typedef struct { const unsigned char *p, *end; int ok; } Cursor;
typedef struct {
    char id[41];
    wchar_t wid[41], *directory;
    wchar_t *executable, *command, *resume_executable, *resume_command;
    uint64_t hard;
    uint32_t resumes_left, attempt;
    HANDLE group;
    PROCESS_INFORMATION process;
    Journal journal;
    HrOutput output;
    ULONGLONG deadline, kill_deadline;
    DWORD exit_code;
    int ack, killed, succeeded;
} Task;
typedef struct {
    Task *tasks;
    HrJob jobs[MAX_JOBS];
    uint32_t count, sustain, grace, timeout;
    uint64_t budget, hard, reserve;
    HANDLE root, lease;
    unsigned char *plan_bytes;
    DWORD plan_length;
    Journal journal;
    wchar_t directory[PATH_CAP], working_directory[PATH_CAP];
    HrIntake *intake;
    int live, sealed;
    uint32_t processed;
} Fleet;

static uint32_t u32(Cursor *r) {
    if (!r->ok || (size_t)(r->end - r->p) < 4) { r->ok = 0; return 0; }
    uint32_t out = (uint32_t)r->p[0] | (uint32_t)r->p[1] << 8
        | (uint32_t)r->p[2] << 16 | (uint32_t)r->p[3] << 24;
    r->p += 4;
    return out;
}
static uint64_t u64(Cursor *r) {
    uint64_t low = u32(r), high = u32(r);
    return low | high << 32;
}
static wchar_t *wide(Cursor *r, uint32_t limit) {
    uint32_t length = u32(r);
    if (!r->ok || length > limit || (size_t)(r->end - r->p) < (size_t)length * 2) {
        r->ok = 0; return NULL;
    }
    wchar_t *out = calloc((size_t)length + 1, sizeof(wchar_t));
    if (!out) { r->ok = 0; return NULL; }
    for (uint32_t i = 0; i < length; ++i) {
        out[i] = (wchar_t)((uint32_t)r->p[i * 2] | (uint32_t)r->p[i * 2 + 1] << 8);
        if (!out[i]) r->ok = 0;
    }
    for (uint32_t i = 0; i < length; ++i) {
        unsigned int unit = (unsigned int)out[i];
        if (unit >= 0xd800 && unit <= 0xdbff) {
            if (i + 1 >= length || out[i + 1] < 0xdc00 || out[i + 1] > 0xdfff) r->ok = 0;
            else ++i;
        } else if (unit >= 0xdc00 && unit <= 0xdfff) r->ok = 0;
    }
    r->p += (size_t)length * 2;
    return out;
}
static int byte_limit(uint64_t value, int zero) {
    return (zero || value) && value <= MAX_BYTES && !(value % MIB);
}
/* Takes ownership of bytes on every path. The live entry point accepts only
 * an empty header; regular batches and incoming requests still need jobs. */
static int parse_plan_owned(unsigned char *bytes, DWORD got, Fleet *f) {
    if (got < 48 || got > MAX_PLAN || memcmp(bytes, "HRFLEET1", 8)) { free(bytes); return 0; }
    Cursor r = {bytes + 8, bytes + got, 1};
    f->count = u32(&r);
    f->budget = u64(&r); f->hard = u64(&r); f->reserve = u64(&r);
    f->sustain = u32(&r); f->grace = u32(&r); f->timeout = u32(&r);
    if ((f->live ? f->count != 0 : !f->count) || f->count > MAX_JOBS || !byte_limit(f->budget, 0)
        || !byte_limit(f->hard, 0) || !byte_limit(f->reserve, 1) || f->hard <= f->budget
        || f->sustain < 100 || f->sustain > 60000 || !f->grace || f->grace > 60000
        || !f->timeout || f->timeout > 86400000) { free(bytes); return 0; }
    f->tasks = calloc(f->live ? MAX_JOBS : f->count, sizeof(Task));
    if (!f->tasks) { free(bytes); return 0; }
    for (uint32_t i = 0; i < f->count && r.ok; ++i) {
        Task *t = f->tasks + i;
        HrJob *j = f->jobs + i;
        t->journal.log = INVALID_HANDLE_VALUE;
        uint32_t length = u32(&r);
        if (!length || length > 40 || (size_t)(r.end - r.p) < length) { r.ok = 0; break; }
        for (uint32_t k = 0; k < length; ++k) {
            unsigned char c = r.p[k];
            if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || (k && c == '-'))) r.ok = 0;
            t->id[k] = (char)c; t->wid[k] = c;
        }
        r.p += length;
        if (!strcmp(t->id, "con") || !strcmp(t->id, "prn") || !strcmp(t->id, "aux") || !strcmp(t->id, "nul")
            || ((length == 4) && (!strncmp(t->id, "com", 3) || !strncmp(t->id, "lpt", 3))
                && t->id[3] >= '1' && t->id[3] <= '9')) r.ok = 0;
        for (uint32_t k = 0; k < i; ++k) if (!strcmp(t->id, f->tasks[k].id)) r.ok = 0;
        j->priority = u32(&r); j->allow_stop = u32(&r); t->resumes_left = u32(&r);
        j->claim = u64(&r); t->hard = u64(&r);
        t->executable = wide(&r, PATH_CAP - 1); t->command = wide(&r, CMD_CAP - 1);
        t->resume_executable = wide(&r, PATH_CAP - 1); t->resume_command = wide(&r, CMD_CAP - 1);
        if (j->priority > 100 || j->allow_stop > 1 || t->resumes_left > 3
            || !byte_limit(j->claim, 0) || !byte_limit(t->hard, 0)
            || j->claim > f->budget || j->claim > t->hard || t->hard > f->hard
            || !t->executable || !*t->executable || !t->command || !*t->command
            || !t->resume_executable || !t->resume_command) r.ok = 0;
        if (r.ok && (t->resumes_left && (!j->allow_stop || !*t->resume_executable || !*t->resume_command))) r.ok = 0;
        if (r.ok && ((!*t->resume_executable) != (!*t->resume_command))) r.ok = 0;
    }
    int ok = r.ok && r.p == r.end;
    if (ok) { f->plan_bytes = bytes; f->plan_length = got; }
    else free(bytes);
    return ok;
}

static int read_plan(const wchar_t *path, Fleet *f) {
    HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT, NULL);
    if (file == INVALID_HANDLE_VALUE) return 0;
    LARGE_INTEGER size;
    BY_HANDLE_FILE_INFORMATION info;
    int ok = GetFileInformationByHandle(file, &info)
        && !(info.dwFileAttributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY))
        && GetFileType(file) == FILE_TYPE_DISK && GetFileSizeEx(file, &size)
        && size.QuadPart >= 48 && size.QuadPart <= MAX_PLAN;
    if (!ok) { CloseHandle(file); return 0; }
    unsigned char *bytes = malloc((size_t)size.QuadPart);
    if (!bytes) { CloseHandle(file); return 0; }
    DWORD got = 0;
    ok = ReadFile(file, bytes, (DWORD)size.QuadPart, &got, NULL) && got == size.QuadPart;
    CloseHandle(file);
    if (!ok) { free(bytes); return 0; }
    return parse_plan_owned(bytes, got, f);
}

static int write_new(const wchar_t *path, const void *body, DWORD length) {
    wchar_t temporary[PATH_CAP];
    size_t units = wcslen(path);
    if (units + 4 >= PATH_CAP) { SetLastError(ERROR_INSUFFICIENT_BUFFER); return 0; }
    wcscpy(temporary, path); wcscat(temporary, L".tmp");
    HANDLE file = CreateFileW(temporary, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_NEW,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_WRITE_THROUGH, NULL);
    if (file == INVALID_HANDLE_VALUE) return 0;
    int ok = write_all(file, body, length);
    DWORD error = ok ? ERROR_SUCCESS : GetLastError();
    if (!CloseHandle(file) && ok) { ok = 0; error = GetLastError(); }
    if (ok && MoveFileExW(temporary, path, MOVEFILE_WRITE_THROUGH)) return 1;
    if (ok) error = GetLastError();
    DeleteFileW(temporary); /* Only this call's exclusively created temporary. */
    SetLastError(error);
    return 0;
}

static int persist_plan(Fleet *f) {
    wchar_t path[PATH_CAP];
    if (!join_path(path, f->directory, L"plan.hrp")) return 0;
    HANDLE file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_NEW,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_WRITE_THROUGH, NULL);
    if (file == INVALID_HANDLE_VALUE) return 0;
    int ok = write_all(file, (const char *)f->plan_bytes, f->plan_length);
    CloseHandle(file);
    if (ok) { free(f->plan_bytes); f->plan_bytes = NULL; }
    return ok;
}

static int persist_working_directory(Fleet *f) {
    DWORD length = GetCurrentDirectoryW(PATH_CAP, f->working_directory);
    wchar_t path[PATH_CAP];
    if (!length || length >= PATH_CAP
        || !join_path(path, f->directory, L"working-directory.hrc")) return 0;
    HANDLE file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_NEW,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_WRITE_THROUGH, NULL);
    if (file == INVALID_HANDLE_VALUE) return 0;
    int ok = write_all(file, "HRCWD001", 8)
        && write_all(file, (const char *)f->working_directory, length * sizeof(wchar_t));
    CloseHandle(file);
    return ok;
}

static int persist_owner(Fleet *f) {
    wchar_t path[PATH_CAP];
    char body[256];
    if (!join_path(path, f->directory, L"owner.json")) return 0;
    int n = snprintf(body, sizeof(body), f->live
        ? "{\"version\":2,\"mode\":\"live\",\"run_id\":\"%s\",\"job_name\":\"Local\\\\Headroom-%s\"}\n"
        : "{\"version\":1,\"run_id\":\"%s\",\"job_name\":\"Local\\\\Headroom-%s\"}\n",
        f->journal.nonce, f->journal.nonce);
    if (n < 0 || (size_t)n >= sizeof(body)) return 0;
    HANDLE file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_NEW,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_WRITE_THROUGH, NULL);
    if (file == INVALID_HANDLE_VALUE) return 0;
    int ok = write_all(file, body, (DWORD)n);
    CloseHandle(file);
    return ok;
}

static int new_journal(Journal *j, const wchar_t *directory, const wchar_t *name) {
    GUID guid;
    wchar_t path[PATH_CAP];
    if (FAILED(CoCreateGuid(&guid)) || !join_path(path, directory, name)) return 0;
    j->started = GetTickCount64();
    snprintf(j->nonce, sizeof(j->nonce), "%08lx-%04x-%04x-%02x%02x-%02x%02x%02x%02x%02x%02x",
        (unsigned long)guid.Data1, guid.Data2, guid.Data3, guid.Data4[0], guid.Data4[1],
        guid.Data4[2], guid.Data4[3], guid.Data4[4], guid.Data4[5], guid.Data4[6], guid.Data4[7]);
    j->log = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_NEW,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_WRITE_THROUGH, NULL);
    return j->log != INVALID_HANDLE_VALUE;
}

static int fleet_event(Fleet *f, const char *name, int index,
                       uint64_t total, uint64_t available, DWORD code) {
    char line[768];
    Task *t = index < 0 ? NULL : f->tasks + index;
    int n = snprintf(line, sizeof(line),
        "{\"version\":1,\"run_id\":\"%s\",\"event\":\"%s\",\"elapsed_ms\":%llu,"
        "\"task\":\"%s\",\"priority\":%u,\"attempt\":%u,\"total_commit_bytes\":%llu,"
        "\"available_bytes\":%llu,\"code\":%lu}\n", f->journal.nonce, name,
        GetTickCount64() - f->journal.started, t ? t->id : "",
        t ? f->jobs[index].priority : 0, t ? t->attempt : 0, total, available, (unsigned long)code);
    return n > 0 && (size_t)n < sizeof(line) && write_all(f->journal.log, line, (DWORD)n);
}

static void close_attempt(Task *t) {
    hr_output_close(&t->output);
    if (t->group) CloseHandle(t->group);
    if (t->process.hThread) CloseHandle(t->process.hThread);
    if (t->process.hProcess) CloseHandle(t->process.hProcess);
    if (t->journal.log != INVALID_HANDLE_VALUE) CloseHandle(t->journal.log);
    t->group = NULL; t->process.hThread = t->process.hProcess = NULL;
    t->journal.log = INVALID_HANDLE_VALUE;
    free(t->directory); t->directory = NULL;
}

static int launch(Fleet *f, uint32_t index) {
    Task *t = f->tasks + index;
    HrJob *j = f->jobs + index;
    wchar_t base[PATH_CAP], attempt[48];
    int first = !t->attempt;
    ++t->attempt;
    if (!join_path(base, f->directory, t->wid)) return 0;
    if (first && !CreateDirectoryW(base, NULL)) return 0;
    swprintf(attempt, 48, L"attempt-%u", t->attempt);
    size_t directory_units = wcslen(base) + wcslen(attempt) + 2;
    if (directory_units > PATH_CAP) { SetLastError(ERROR_FILENAME_EXCED_RANGE); return 0; }
    t->directory = malloc(directory_units * sizeof(wchar_t));
    if (!t->directory) { SetLastError(ERROR_NOT_ENOUGH_MEMORY); return 0; }
    if (swprintf(t->directory, directory_units, L"%ls\\%ls", base, attempt) < 0
        || !CreateDirectoryW(t->directory, NULL)
        || !new_journal(&t->journal, t->directory, L"events.jsonl")
        || !event(&t->journal, "prepared", 0, 0, 0)) return 0;
    t->group = CreateJobObjectW(NULL, NULL);
    if (!t->group) return 0;
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = {0};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_JOB_MEMORY;
    limits.JobMemoryLimit = (SIZE_T)t->hard;
    if (!SetInformationJobObject(t->group, JobObjectExtendedLimitInformation, &limits, sizeof(limits))) return 0;
    wchar_t *exe = t->attempt == 1 ? t->executable : t->resume_executable;
    wchar_t *original = t->attempt == 1 ? t->command : t->resume_command;
    wchar_t *command = _wcsdup(original);
    if (!command) return 0;
    if (!SetEnvironmentVariableW(L"HEADROOM_CONTROL_DIR", t->directory)) { free(command); return 0; }
    STARTUPINFOEXW startup;
    if (!hr_output_prepare(&t->output, t->directory, &startup)) { free(command); return 0; }
    HANDLE groups[2] = {f->root, t->group};
    if (!UpdateProcThreadAttribute(startup.lpAttributeList, 0,
        PROC_THREAD_ATTRIBUTE_JOB_LIST, groups, sizeof(groups), NULL, NULL)) {
        free(command); return 0;
    }
    /* Kernel assignment in creation closes even the suspended create/assign
       controller-crash window. Job handles themselves are not inherited. */
    BOOL created = CreateProcessW(exe, command, NULL, NULL, TRUE,
        CREATE_SUSPENDED | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT,
        NULL, f->working_directory, &startup.StartupInfo, &t->process);
    DWORD create_error = GetLastError();
    hr_output_child_done(&t->output);
    free(command);
    if (!created) { SetLastError(create_error); return 0; }
    if (!event(&t->journal, "contained", 0, 1, t->process.dwProcessId)
        || !fleet_event(f, "started", index, 0, 0, t->process.dwProcessId)) return 0;
    if (ResumeThread(t->process.hThread) == (DWORD)-1) return 0;
    CloseHandle(t->process.hThread); t->process.hThread = NULL;
    j->state = HR_RUNNING; j->valid = 1; j->commit = j->working_set = 0;
    t->ack = t->killed = 0; t->deadline = t->kill_deadline = 0;
    return 1;
}

/* A broken executable or a rejected nested job must not destroy peers which
 * are already running. Escalate to fleet failure only if cleanup or durable
 * attribution itself fails. No process can have spawned children before
 * ResumeThread; if it did resume, the owned nested job remains the boundary. */
static int fail_start(Fleet *f, uint32_t index, DWORD error) {
    Task *t = f->tasks + index;
    HrJob *j = f->jobs + index;
    if (t->process.hProcess && WaitForSingleObject(t->process.hProcess, 0) != WAIT_OBJECT_0) {
        if (!TerminateProcess(t->process.hProcess, 70)
            || WaitForSingleObject(t->process.hProcess, 5000) != WAIT_OBJECT_0) return 0;
    }
    if (t->group) {
        JOBOBJECT_BASIC_ACCOUNTING_INFORMATION accounting = {0};
        if (!QueryInformationJobObject(t->group, JobObjectBasicAccountingInformation,
                                       &accounting, sizeof(accounting), NULL) || accounting.ActiveProcesses) return 0;
    }
    if (t->journal.log != INVALID_HANDLE_VALUE
        && !event(&t->journal, "guardian_error", 0, 0, error ? error : ERROR_GEN_FAILURE)) return 0;
    if (!fleet_event(f, "launch_failed", index, 0, 0, 70)) return 0;
    close_attempt(t);
    j->state = HR_DONE; j->valid = 1; j->commit = j->working_set = 0;
    t->exit_code = 70; t->succeeded = 0;
    return 1;
}

/* Read only kernel-named members and pin each queried identity with a handle.
 * Measurement failure is not an empty job and must never authorize an action. */
static int sample(Task *t, HrJob *j, DWORD *active) {
    JOBOBJECT_BASIC_ACCOUNTING_INFORMATION accounting;
    if (!QueryInformationJobObject(t->group, JobObjectBasicAccountingInformation,
                                   &accounting, sizeof(accounting), NULL)) return 0;
    *active = accounting.ActiveProcesses;
    j->commit = j->working_set = 0;
    if (!*active) { j->valid = 1; return 1; }
    size_t capacity = 64;
    JOBOBJECT_BASIC_PROCESS_ID_LIST *list = NULL;
    int ok = 0;
    while (capacity <= 4096) {
        size_t size = sizeof(*list) + (capacity - 1) * sizeof(ULONG_PTR);
        list = calloc(1, size);
        if (!list) return 0;
        if (QueryInformationJobObject(t->group, JobObjectBasicProcessIdList, list, (DWORD)size, NULL)) {
            ok = list->NumberOfProcessIdsInList <= capacity
                && list->NumberOfAssignedProcesses == list->NumberOfProcessIdsInList;
            break;
        }
        DWORD error = GetLastError();
        free(list); list = NULL;
        if (error != ERROR_MORE_DATA) return 0;
        capacity *= 2;
    }
    if (!ok || !list) { free(list); return 0; }
    for (DWORD i = 0; i < list->NumberOfProcessIdsInList; ++i) {
        HANDLE process = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, FALSE, (DWORD)list->ProcessIdList[i]);
        if (!process) { ok = 0; break; }
        BOOL belongs = FALSE;
        PROCESS_MEMORY_COUNTERS_EX memory = {0}; memory.cb = sizeof(memory);
        int valid = IsProcessInJob(process, t->group, &belongs) && belongs
            && GetProcessMemoryInfo(process, (PROCESS_MEMORY_COUNTERS *)&memory, sizeof(memory));
        CloseHandle(process);
        if (!valid || UINT64_MAX - j->commit < memory.PrivateUsage
            || UINT64_MAX - j->working_set < memory.WorkingSetSize) { ok = 0; break; }
        j->commit += memory.PrivateUsage; j->working_set += memory.WorkingSetSize;
    }
    free(list);
    j->valid = ok;
    return ok;
}

static int finish_task(Fleet *f, uint32_t i, uint64_t total, uint64_t available) {
    Task *t = f->tasks + i; HrJob *j = f->jobs + i;
    DWORD code;
    /* ActiveProcesses was zero. 259 can also be a legitimate completed code. */
    if (!GetExitCodeProcess(t->process.hProcess, &code)) return 0;
    int stopped = j->state == HR_STOPPING;
    if (stopped && !t->ack && acknowledged(t->directory, &t->journal)) {
        t->ack = 1;
        if (!event(&t->journal, "checkpoint_acknowledged", j->commit, 0, 0)
            || !fleet_event(f, "checkpoint_acknowledged", i, total, available, 0)) return 0;
    }
    /* The task can flush its ack and exit before the bounded read worker
     * completes. Keep the original grace window to collect it; do not turn a
     * transient 5 ms read miss into a permanent loss of supported resume. */
    if (stopped && !t->ack && GetTickCount64() < t->deadline) return 2;
    int output_finished = hr_output_finish(&t->output, t->directory);
    if (output_finished != 1) return output_finished;
    if (!event(&t->journal, stopped ? "stopped" : "completed", j->commit, 0, code)
        || !fleet_event(f, stopped ? "stopped" : "completed", i, total, available, code)) return 0;
    t->exit_code = stopped ? 125 : code;
    t->succeeded = !stopped && code == 0;
    int resume = stopped && t->ack && t->resumes_left;
    close_attempt(t);
    j->commit = j->working_set = 0; j->valid = 1;
    if (resume) {
        --t->resumes_left;
        if (!fleet_event(f, "resume_queued", i, total, available, t->resumes_left)) return 0;
        j->state = HR_QUEUED;
    } else j->state = HR_DONE;
    return 1;
}

static int fresh_choice(Fleet *f, int expected, uint64_t *total, uint64_t *available) {
    MEMORYSTATUSEX host = {0}; host.dwLength = sizeof(host);
    if (!GlobalMemoryStatusEx(&host)) return 0;
    *total = 0; *available = host.ullAvailPhys;
    for (uint32_t i = 0; i < f->count; ++i) {
        HrJob *j = f->jobs + i;
        if (j->state != HR_RUNNING && j->state != HR_STOPPING) continue;
        DWORD active = 0;
        if (!sample(f->tasks + i, j, &active) || !active || UINT64_MAX - *total < j->commit) return 0;
        *total += j->commit;
    }
    return hr_victim(f->jobs, f->count, *total, f->budget, *available, f->reserve, 1) == expected;
}

static void dispose(Fleet *f) {
    hr_intake_close(f->intake);
    /* Closing the root first contains even a half-constructed nested attempt. */
    if (f->root) CloseHandle(f->root);
    if (f->tasks) {
        for (uint32_t i = 0; i < f->count; ++i) {
            Task *t = f->tasks + i;
            close_attempt(t);
            free(t->executable); free(t->command);
            free(t->resume_executable); free(t->resume_command);
        }
    }
    if (f->journal.log != INVALID_HANDLE_VALUE) CloseHandle(f->journal.log);
    if (f->lease && f->lease != INVALID_HANDLE_VALUE) CloseHandle(f->lease);
    free(f->plan_bytes);
    free(f->tasks);
}

static int quiesce(Fleet *f) {
    ULONGLONG deadline = GetTickCount64() + 5000;
    int ok = 1;
    if (f->root) {
        if (!TerminateJobObject(f->root, 70)) ok = 0;
        for (;;) {
            JOBOBJECT_BASIC_ACCOUNTING_INFORMATION accounting = {0};
            if (!QueryInformationJobObject(f->root, JobObjectBasicAccountingInformation,
                                           &accounting, sizeof(accounting), NULL)) { ok = 0; break; }
            if (!accounting.ActiveProcesses) break;
            if (GetTickCount64() >= deadline) { ok = 0; break; }
            Sleep(10);
        }
    }
    /* A launch may have failed before root assignment. Its newly created
     * process handle is still ours, even when no job contains it yet. */
    for (uint32_t i = 0; i < f->count; ++i) {
        HANDLE process = f->tasks[i].process.hProcess;
        if (!process || WaitForSingleObject(process, 0) == WAIT_OBJECT_0) continue;
        if (!TerminateProcess(process, 70)) { ok = 0; continue; }
        ULONGLONG now = GetTickCount64();
        DWORD remaining = now < deadline ? (DWORD)(deadline - now) : 0;
        if (WaitForSingleObject(process, remaining) != WAIT_OBJECT_0) ok = 0;
    }
    return ok;
}

static int start_live(Fleet *f) {
    const wchar_t *names[3] = {L"inbox", L"processed", L"receipts"};
    wchar_t path[PATH_CAP];
    for (int i = 0; i < 3; ++i)
        if (!join_path(path, f->directory, names[i]) || !CreateDirectoryW(path, NULL)) return 0;
    f->intake = hr_intake_start(f->directory, f->journal.nonce);
    if (!f->intake || !fleet_event(f, "listening", -1, 0, 0, 0)) return 0;
    char body[768];
    int n = snprintf(body, sizeof(body),
        "{\"version\":1,\"run_id\":\"%s\",\"max_jobs\":64,\"settings\":{"
        "\"budget_mib\":%llu,\"hard_mib\":%llu,\"reserve_mib\":%llu,"
        "\"sustain_ms\":%u,\"grace_ms\":%u,\"timeout_ms\":%u}}\n",
        f->journal.nonce, (unsigned long long)(f->budget / MIB),
        (unsigned long long)(f->hard / MIB), (unsigned long long)(f->reserve / MIB),
        f->sustain, f->grace, f->timeout);
    return n > 0 && (size_t)n < sizeof(body)
        && join_path(path, f->directory, L"session.json") && write_new(path, body, (DWORD)n);
}

static int seal_live(Fleet *f) {
    if (f->sealed) return 1;
    wchar_t path[PATH_CAP];
    char body[128];
    int n = snprintf(body, sizeof(body), "{\"version\":1,\"run_id\":\"%s\"}\n", f->journal.nonce);
    if (n <= 0 || n >= (int)sizeof(body) || !join_path(path, f->directory, L"sealed.json")
        || !write_new(path, body, (DWORD)n) || !fleet_event(f, "sealed", -1, 0, 0, 0)) return 0;
    f->sealed = 1;
    hr_intake_close(f->intake); f->intake = NULL;
    return 1;
}

static int receipt(Fleet *f, HrIntakeResult *r, const char *task, DWORD code, uint32_t ordinal) {
    wchar_t base[PATH_CAP], name[48], path[PATH_CAP];
    char body[384];
    int n = snprintf(body, sizeof(body),
        "{\"version\":1,\"run_id\":\"%s\",\"request_id\":\"%s\",\"task\":\"%s\","
        "\"status\":\"%s\",\"code\":%lu,\"ordinal\":%u}\n",
        f->journal.nonce, r->request_id, task, code ? "rejected" : "accepted",
        (unsigned long)code, ordinal);
    if (n <= 0 || n >= (int)sizeof(body)
        || swprintf(name, 48, L"%hs.json", r->request_id) < 0) return 0;
    return join_path(base, f->directory, L"receipts") && join_path(path, base, name)
        && write_new(path, body, (DWORD)n);
}

static int accept_claim(Fleet *f, HrIntakeResult *r) {
    DWORD rejection = r->code ? 64 : 0;
    Fleet *incoming = NULL;
    if (!rejection && f->count >= MAX_JOBS) rejection = 75;
    if (!rejection && (!r->plan || r->length < 48 || memcmp(r->plan, "HRFLEET1", 8)
        || r->plan[8] != 1 || r->plan[9] || r->plan[10] || r->plan[11])) rejection = 64;
    if (!rejection) {
        incoming = calloc(1, sizeof(*incoming));
        if (!incoming) { SetLastError(ERROR_NOT_ENOUGH_MEMORY); return 0; }
        incoming->journal.log = INVALID_HANDLE_VALUE;
        unsigned char *bytes = r->plan; r->plan = NULL;
        if (!parse_plan_owned(bytes, r->length, incoming)) rejection = 64;
    }
    if (!rejection && (incoming->budget != f->budget || incoming->hard != f->hard
        || incoming->reserve != f->reserve || incoming->sustain != f->sustain
        || incoming->grace != f->grace || incoming->timeout != f->timeout)) rejection = 64;
    if (!rejection) {
        for (uint32_t i = 0; i < f->count; ++i)
            if (!strcmp(f->tasks[i].id, incoming->tasks[0].id)) rejection = 64;
    }
    if (rejection) {
        if (incoming) { dispose(incoming); free(incoming); }
        return fleet_event(f, "request_rejected", -1, 0, 0, rejection)
            && receipt(f, r, "", rejection, 0);
    }
    wchar_t path[PATH_CAP];
    if (!join_path(path, r->directory, L"validated.hrp")
        || !write_new(path, incoming->plan_bytes, incoming->plan_length)) {
        dispose(incoming); free(incoming);
        /* Request-local collisions/permissions are not authority to kill
         * previously accepted peers. Only failed ROOT attribution escalates. */
        return fleet_event(f, "request_rejected", -1, 0, 0, 73)
            && receipt(f, r, "", 73, 0);
    }
    uint32_t index = f->count++;
    f->tasks[index] = incoming->tasks[0];
    f->jobs[index] = incoming->jobs[0];
    memset(incoming->tasks, 0, sizeof(Task));
    incoming->tasks[0].journal.log = INVALID_HANDLE_VALUE;
    dispose(incoming); free(incoming);
    return fleet_event(f, "queued", (int)index, 0, 0, 0)
        && receipt(f, r, f->tasks[index].id, 0, index + 1);
}

int wmain(int argc, wchar_t **argv) {
    const wchar_t *plan = NULL, *state = NULL;
    int live = 0;
    for (int i = 1; i < argc; i += 2) {
        if (i + 1 >= argc) return 64;
        if (!wcscmp(argv[i], L"--plan") && !plan) plan = argv[i + 1];
        else if (!wcscmp(argv[i], L"--live-plan") && !plan) { plan = argv[i + 1]; live = 1; }
        else if (!wcscmp(argv[i], L"--state") && !state) state = argv[i + 1];
        else return 64;
    }
    if (!plan || !state) {
        fputs("headroom-fleet --plan COMPILED_PLAN --state NEW_DIRECTORY\n", stderr); return 64;
    }
    Fleet *f = calloc(1, sizeof(Fleet));
    if (!f) return 70;
    f->journal.log = INVALID_HANDLE_VALUE;
    f->live = live;
    if (!read_plan(plan, f)) {
        fputs("Invalid fleet plan. No state or job was created.\n", stderr);
        dispose(f); free(f); return 64;
    }
    DWORD length = GetFullPathNameW(state, PATH_CAP, f->directory, NULL);
    if (!length || length >= PATH_CAP || !CreateDirectoryW(f->directory, NULL)) {
        dispose(f); free(f); return 73;
    }
    int result = 70, handler_registered = 0;
    wchar_t lease_path[PATH_CAP];
    if (!join_path(lease_path, f->directory, L"owner.lock")) goto end;
    f->lease = CreateFileW(lease_path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ,
        NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
    if (f->lease == INVALID_HANDLE_VALUE) goto end;
    if (!new_journal(&f->journal, f->directory, L"fleet.jsonl")
        || !persist_plan(f) || !persist_working_directory(f) || !persist_owner(f)
        || !fleet_event(f, "prepared", -1, 0, 0, 0)) goto end;
    wchar_t job_name[96];
    if (swprintf(job_name, 96, L"Local\\Headroom-%hs", f->journal.nonce) < 0) goto end;
    f->root = CreateJobObjectW(NULL, job_name);
    if (f->root && GetLastError() == ERROR_ALREADY_EXISTS) {
        CloseHandle(f->root); f->root = NULL; goto end;
    }
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = {0};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_JOB_MEMORY;
    limits.JobMemoryLimit = (SIZE_T)f->hard;
    if (!f->root || !SetInformationJobObject(f->root, JobObjectExtendedLimitInformation, &limits, sizeof(limits))) goto end;
    for (uint32_t i = 0; i < f->count; ++i)
        if (!fleet_event(f, "queued", i, 0, 0, 0)) goto end;
    if (!SetConsoleCtrlHandler(control, TRUE)) goto end;
    handler_registered = 1;
    if (f->live && !start_live(f)) goto end;
    ULONGLONG pressure_since = 0, last_sample = 0, last_report = 0;
    ULONGLONG next_intake = 0;
    int unresolved_reported = 0;
    for (;;) {
        ULONGLONG now = GetTickCount64();
        if (InterlockedCompareExchange(&interrupted, 0, 0)
            || now - f->journal.started >= f->timeout) {
            int cancelled = InterlockedCompareExchange(&interrupted, 0, 0) != 0;
            result = cancelled ? 130 : 124;
            if (!fleet_event(f, cancelled ? "interrupt" : "deadline", -1, 0, 0, (DWORD)result)) result = 70;
            break;
        }
        if (f->intake) {
            HrIntakeResult input = {0};
            int received = hr_intake_poll(f->intake, &input);
            if (received < 0) goto end;
            if (received) {
                int accepted = 1;
                if (input.kind == 3) { SetLastError(input.code); accepted = 0; }
                else if (input.kind == 2) accepted = seal_live(f);
                else if (input.kind == 1) {
                    ++f->processed;
                    accepted = accept_claim(f, &input);
                    if (accepted && (f->count == MAX_JOBS || f->processed == 128)) accepted = seal_live(f);
                }
                free(input.plan);
                if (!accepted) goto end;
            }
            if (f->intake && now >= next_intake) {
                if (!hr_intake_schedule(f->intake)) goto end;
                next_intake = now + 250;
            }
        }
        uint64_t total = 0;
        MEMORYSTATUSEX host = {0}; host.dwLength = sizeof(host);
        int valid = GlobalMemoryStatusEx(&host), pending = 0, output_busy = 0;
        uint32_t done = 0;
        DWORD active[MAX_JOBS] = {0};
        /* Measure the complete cycle before recording totals or policy actions. */
        for (uint32_t i = 0; i < f->count; ++i) {
            HrJob *j = f->jobs + i; Task *t = f->tasks + i;
            if (j->state == HR_RUNNING || j->state == HR_STOPPING) {
                if (!hr_output_drain(&t->output, &output_busy)) goto end;
                if (!sample(t, j, &active[i])) { j->valid = 0; valid = 0; }
                if (UINT64_MAX - total < j->commit) valid = 0;
                else total += j->commit;
            }
        }
        for (uint32_t i = 0; i < f->count; ++i) {
            HrJob *j = f->jobs + i; Task *t = f->tasks + i;
            if (j->state == HR_RUNNING || j->state == HR_STOPPING) {
                if (j->valid && !active[i]) {
                    int finished = finish_task(f, i, total, host.ullAvailPhys);
                    if (!finished) goto end;
                    if (finished == 1) pressure_since = 0;
                }
                if (j->state == HR_STOPPING) {
                    pending = 1;
                    if (j->valid && !active[i]) continue;
                    if (!t->ack && acknowledged(t->directory, &t->journal)) {
                        t->ack = 1;
                        if (!event(&t->journal, "checkpoint_acknowledged", j->commit, active[i], 0)
                            || !fleet_event(f, "checkpoint_acknowledged", i, total, host.ullAvailPhys, 0)) goto end;
                    }
                    if (!t->killed && now >= t->deadline) {
                        if (!event(&t->journal, "terminate_intent", j->commit, active[i], 125)
                            || !fleet_event(f, "terminated", i, total, host.ullAvailPhys, 125)
                            || !TerminateJobObject(t->group, 125)) goto end;
                        t->killed = 1; t->kill_deadline = now + 5000;
                    }
                    if (t->killed && now >= t->kill_deadline) goto end;
                }
            }
            if (j->state == HR_DONE) ++done;
        }
        /* Measurement, output and journal I/O can consume time. Recheck at
           terminal publication; a pending deadline/interrupt is not success. */
        now = GetTickCount64();
        if (InterlockedCompareExchange(&interrupted, 0, 0)) {
            if (!fleet_event(f, "interrupt", -1, total, host.ullAvailPhys, 130)) goto end;
            result = 130; break;
        }
        if (now - f->journal.started >= f->timeout) {
            if (!fleet_event(f, "deadline", -1, total, host.ullAvailPhys, 124)) goto end;
            result = 124; break;
        }
        if (done == f->count && (!f->live || f->sealed)) {
            result = 0;
            for (uint32_t i = 0; i < f->count; ++i) if (!f->tasks[i].succeeded) result = 1;
            if (!fleet_event(f, "finished", -1, total, host.ullAvailPhys, (DWORD)result)) result = 70;
            break;
        }
        if (now - last_report >= 1000) {
            if (!fleet_event(f, valid ? "sample" : "measurement_unavailable", -1, total, host.ullAvailPhys, 0)) goto end;
            last_report = now;
        }
        int pressure = valid && (total > f->budget || host.ullAvailPhys < f->reserve);
        if (!pressure) unresolved_reported = 0;
        if (!pressure || !last_sample || now - last_sample > 1500) pressure_since = 0;
        if (pressure && !pressure_since) pressure_since = now;
        last_sample = now;
        if (pressure && !pending && now - pressure_since >= f->sustain) {
            int victim = hr_victim(f->jobs, f->count, total, f->budget, host.ullAvailPhys, f->reserve, valid);
            if (victim >= 0) {
                Task *t = f->tasks + victim;
                HrJob *j = f->jobs + victim;
                uint64_t current_total, current_available;
                if (fresh_choice(f, victim, &current_total, &current_available)) {
                    if (!fleet_event(f, "pressure_intent", victim, current_total, current_available, 125)
                        || !event(&t->journal, "checkpoint_intent", j->commit, active[victim], 125)) goto end;
                    /* Disk flushes can be slow. A completed/recovered fleet
                     * revokes the intent rather than publishing an old stop. */
                    if (fresh_choice(f, victim, &current_total, &current_available)) {
                        if (!request_stop(t->directory, &t->journal)) goto end;
                        j->state = HR_STOPPING;
                        t->deadline = GetTickCount64() + f->grace;
                        pending = 1;
                    } else {
                        if (!fleet_event(f, "pressure_revoked", victim, current_total, current_available, 0)) goto end;
                        pressure_since = 0;
                    }
                } else pressure_since = 0;
            } else if (!unresolved_reported) {
                if (!fleet_event(f, "pressure_unresolved", -1, total, host.ullAvailPhys, 0)) goto end;
                unresolved_reported = 1;
            }
        }
        if (!pressure && !pending) {
            /* A new process may already consume more than its claim before
             * CreateProcess/ResumeThread returns. Re-measure the entire fleet
             * and host before considering the next queued admission. */
            int next = hr_next(f->jobs, f->count, f->budget, host.ullAvailPhys, f->reserve, valid);
            if (next >= 0) {
                if (!launch(f, (uint32_t)next) && !fail_start(f, (uint32_t)next, GetLastError())) goto end;
            }
        }
        Sleep(output_busy ? 25 : pending ? 100 : 250);
    }
end:
    DWORD originating_error = GetLastError();
    if (result == 70 || result == 124 || result == 130) {
        if (!quiesce(f)) {
            fputs("Owned fleet cleanup could not be confirmed; handle close will retry containment.\n", stderr);
            result = 70;
        }
    }
    if (result == 70) {
        DWORD error = originating_error ? originating_error : ERROR_GEN_FAILURE;
        if (f->journal.log != INVALID_HANDLE_VALUE) fleet_event(f, "internal_error", -1, 0, 0, error);
        fprintf(stderr, "Fleet coordinator failed (Windows error %lu); owned jobs are closed.\n", (unsigned long)error);
    }
    if (handler_registered) SetConsoleCtrlHandler(control, FALSE);
    dispose(f); free(f);
    return result;
}
