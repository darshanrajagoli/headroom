/* Read-only Windows Claude observation. Never assigns, stops or writes to Claude.
 * Only the observer's own bounded Python helper belongs to a cleanup Job Object.
 */
#define _WIN32_WINNT 0x0A00
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <psapi.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>
#include <limits.h>
#include <errno.h>

#define NODES 4096
#define GROUPS 16
#define PATH_UNITS 4096
#define LOG_CAP (64ULL * 1024 * 1024)
typedef unsigned long long U64;
typedef struct {
    DWORD pid, parent, root;
    U64 born, cpu, private_bytes, working_set, root_born;
    int claude, selected, valid, excluded;
} Node;
typedef struct {
    DWORD pid, parent, count;
    U64 born, cpu, private_bytes, working_set;
} Group;
static Node *nodes, *previous;
static Group groups[GROUPS], previous_groups[GROUPS];
static size_t count, previous_count, group_count, previous_group_count;
static HANDLE log_file = INVALID_HANDLE_VALUE;
static HANDLE helper_log = INVALID_HANDLE_VALUE;
static U64 written;
static U64 snapshot_started;
typedef union { TOKEN_USER alignment; BYTE bytes[sizeof(TOKEN_USER) + SECURITY_MAX_SID_SIZE]; } UserToken;
static UserToken own_user;
static int process_user(HANDLE process, UserToken *user) {
    HANDLE token = NULL;
    DWORD bytes = 0;
    if (!OpenProcessToken(process, TOKEN_QUERY, &token)) return 0;
    int ok = GetTokenInformation(token, TokenUser, user, sizeof(*user), &bytes);
    CloseHandle(token);
    return ok;
}

static U64 ft(FILETIME value) { return ((U64)value.dwHighDateTime << 32) | value.dwLowDateTime; }
static U64 now_ms(void) {
    FILETIME value; GetSystemTimeAsFileTime(&value);
    return (ft(value) - 116444736000000000ULL) / 10000;
}
static int join(wchar_t *out, const wchar_t *directory, const wchar_t *name) {
    return swprintf(out, PATH_UNITS, L"%ls\\%ls", directory, name) > 0;
}
static int local_directory(const wchar_t *directory) {
    wchar_t absolute[PATH_UNITS];
    if (wcslen(directory) < 4 || directory[1] != L':' || directory[2] != L'\\') return 0;
    DWORD length = GetFullPathNameW(directory, PATH_UNITS, absolute, NULL);
    if (!length || length >= PATH_UNITS || wcscmp(absolute, directory)) return 0;
    wchar_t drive[4] = {absolute[0], L':', L'\\', 0};
    UINT kind = GetDriveTypeW(drive);
    if (kind != DRIVE_FIXED && kind != DRIVE_REMOVABLE && kind != DRIVE_RAMDISK) return 0;
    for (size_t i = 3; ; ++i) {
        if (absolute[i] && absolute[i] != L'\\') continue;
        wchar_t saved = absolute[i]; absolute[i] = 0;
        if (i && (absolute[i - 1] == L'.' || absolute[i - 1] == L' ')) return 0;
        DWORD attributes = GetFileAttributesW(absolute);
        if (attributes == INVALID_FILE_ATTRIBUTES || !(attributes & FILE_ATTRIBUTE_DIRECTORY)
            || (attributes & FILE_ATTRIBUTE_REPARSE_POINT)) return 0;
        absolute[i] = saved;
        if (!saved) break;
    }
    return 1;
}
static int regular_file(HANDLE file) {
    BY_HANDLE_FILE_INFORMATION info;
    return GetFileType(file) == FILE_TYPE_DISK && GetFileInformationByHandle(file, &info)
        && info.nNumberOfLinks == 1 && !(info.dwFileAttributes &
             (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT));
}
static int emit(const char *data) {
    size_t length = strlen(data);
    if (length > LOG_CAP - written) return 0;
    DWORD sent = 0;
    while (length) {
        DWORD part = 0;
        if (!WriteFile(log_file, data + sent, (DWORD)length, &part, NULL) || !part) return 0;
        length -= part; sent += part; written += part;
    }
    return 1;
}
static int find_node(Node *list, size_t length, DWORD pid) {
    for (size_t i = 0; i < length; ++i) if (list[i].pid == pid) return (int)i;
    return -1;
}
static int query(Node *node) {
    /* Modern Windows exposes counters with query-only rights. No memory-read,
       write, quota, suspend or terminate right is requested for Claude. */
    HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, FALSE, node->pid);
    if (!process) return 0;
    UserToken user;
    DWORD session = 0, own_session = 0;
    if (!process_user(process, &user) || !EqualSid(user.alignment.User.Sid, own_user.alignment.User.Sid)
        || !ProcessIdToSessionId(node->pid, &session)
        || !ProcessIdToSessionId(GetCurrentProcessId(), &own_session) || session != own_session) {
        CloseHandle(process); return 0;
    }
    FILETIME created, exited, kernel_time, user_time;
    PROCESS_MEMORY_COUNTERS_EX memory = {0}; memory.cb = sizeof(memory);
    int ok = GetProcessTimes(process, &created, &exited, &kernel_time, &user_time)
        && GetProcessMemoryInfo(process, (PROCESS_MEMORY_COUNTERS *)&memory, sizeof(memory));
    /* Snapshot names can be executable aliases (Claude's versioned launcher is
       one). Use the creation identity to exclude PID reuse after the snapshot,
       rather than incorrectly dropping an alias whose image basename differs.
       Parent links remain best-effort observations, never control authority. */
    if (ok) ok = ft(created) <= snapshot_started
        && WaitForSingleObject(process, 0) == WAIT_TIMEOUT;
    if (ok) {
        node->born = ft(created); node->cpu = ft(kernel_time) + ft(user_time);
        node->private_bytes = memory.PrivateUsage; node->working_set = memory.WorkingSetSize;
        node->valid = 1;
    }
    CloseHandle(process);
    return ok;
}
static int sample(int *partial, int *foreground) {
    count = group_count = 0; *partial = *foreground = 0;
    FILETIME captured; GetSystemTimeAsFileTime(&captured); snapshot_started = ft(captured);
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE) return 0;
    PROCESSENTRY32W entry = {0}; entry.dwSize = sizeof(entry);
    BOOL available = Process32FirstW(snapshot, &entry);
    while (available) {
        if (count == NODES) { *partial = 1; break; }
        Node *node = nodes + count++;
        memset(node, 0, sizeof(*node));
        node->pid = entry.th32ProcessID; node->parent = entry.th32ParentProcessID;
        node->excluded = node->pid == GetCurrentProcessId();
        node->claude = node->selected = !_wcsicmp(entry.szExeFile, L"claude.exe");
        if (node->claude) {
            DWORD session = 0, own_session = 0;
            if (!ProcessIdToSessionId(node->pid, &session)
                || !ProcessIdToSessionId(GetCurrentProcessId(), &own_session)
                || session != own_session) node->claude = node->selected = 0;
        }
        available = Process32NextW(snapshot, &entry);
    }
    if (!count || (available == FALSE && GetLastError() != ERROR_NO_MORE_FILES)) *partial = 1;
    CloseHandle(snapshot);
    int changed;
    unsigned int depth = 0;
    /* A Claude-launched observer is itself a reported descendant. Exclude our
       whole tree before propagating selection, including our Python helpers. */
    do {
        changed = 0;
        for (size_t i = 0; i < count; ++i) if (!nodes[i].excluded) {
            int parent = find_node(nodes, count, nodes[i].parent);
            if (parent >= 0 && nodes[parent].excluded) nodes[i].excluded = changed = 1;
        }
    } while (changed && ++depth < 128);
    if (changed) *partial = 1;
    for (size_t i = 0; i < count; ++i)
        if (nodes[i].excluded) nodes[i].claude = nodes[i].selected = 0;
    depth = 0;
    do {
        changed = 0;
        for (size_t i = 0; i < count; ++i) if (!nodes[i].selected && !nodes[i].excluded) {
            int parent = find_node(nodes, count, nodes[i].parent);
            if (parent >= 0 && nodes[parent].selected) nodes[i].selected = changed = 1;
        }
    } while (changed && ++depth < 128);
    if (changed) *partial = 1;
    for (size_t i = 0; i < count; ++i)
        if (nodes[i].selected && !query(nodes + i)) *partial = 1;
    DWORD focused_pid = 0;
    GetWindowThreadProcessId(GetForegroundWindow(), &focused_pid);
    for (size_t i = 0; i < count; ++i) {
        Node *node = nodes + i;
        if (!node->selected || !node->valid) continue;
        int current = (int)i, root = node->claude ? (int)i : -1;
        unsigned int hops = 0;
        while (++hops < 128) {
            int parent = find_node(nodes, count, nodes[current].parent);
            if (parent < 0 || !nodes[parent].selected) break;
            if (!nodes[parent].valid || nodes[parent].born > nodes[current].born || parent == current) {
                *partial = 1; break;
            }
            current = parent;
            if (nodes[current].claude) root = current;
        }
        if (hops == 128) { *partial = 1; continue; }
        if (root < 0) { *partial = 1; continue; }
        node->root = nodes[root].pid; node->root_born = nodes[root].born;
        size_t slot = 0;
        while (slot < group_count && (groups[slot].pid != node->root || groups[slot].born != node->root_born)) ++slot;
        if (slot == group_count) {
            if (group_count == GROUPS) { *partial = 1; continue; }
            memset(groups + slot, 0, sizeof(groups[slot]));
            groups[slot].pid = node->root; groups[slot].born = node->root_born;
            groups[slot].parent = nodes[root].parent;
            for (size_t j = 0; j < previous_group_count; ++j)
                if (previous_groups[j].pid == node->root && previous_groups[j].born == node->root_born)
                    groups[slot].cpu = previous_groups[j].cpu;
            ++group_count;
        }
        Group *group = groups + slot;
        group->private_bytes += node->private_bytes; group->working_set += node->working_set;
        ++group->count;
        int old = find_node(previous, previous_count, node->pid);
        if (old >= 0 && previous[old].valid && previous[old].born == node->born
            && previous[old].root == node->root && previous[old].root_born == node->root_born
            && node->cpu >= previous[old].cpu) group->cpu += node->cpu - previous[old].cpu;
        if (node->pid == focused_pid) *foreground = 1;
    }
    memcpy(previous, nodes, count * sizeof(*nodes)); previous_count = count;
    memcpy(previous_groups, groups, group_count * sizeof(*groups)); previous_group_count = group_count;
    return 1;
}
static int emit_sample(void) {
    int partial, focused;
    if (!sample(&partial, &focused)) { partial = 1; focused = 0; group_count = 0; }
    MEMORYSTATUSEX host = {0}; host.dwLength = sizeof(host);
    if (!GlobalMemoryStatusEx(&host)) partial = 1;
    PROCESS_MEMORY_COUNTERS_EX own = {0}; own.cb = sizeof(own);
    FILETIME born, gone, kernel_time = {0}, user_time = {0};
    if (!GetProcessMemoryInfo(GetCurrentProcess(), (PROCESS_MEMORY_COUNTERS *)&own, sizeof(own))
        || !GetProcessTimes(GetCurrentProcess(), &born, &gone, &kernel_time, &user_time)) partial = 1;
    char buffer[1024];
    snprintf(buffer, sizeof(buffer), "{\"type\":\"sample\",\"ts_ms\":%llu,\"host_total_bytes\":%llu,"
        "\"host_available_bytes\":%llu,\"collector_private_bytes\":%llu,\"collector_working_set_bytes\":%llu,"
        "\"collector_cpu_100ns\":%llu,\"claude_foreground\":%s,\"snapshot_incomplete\":%s,\"processes\":[",
        now_ms(), (U64)host.ullTotalPhys, (U64)host.ullAvailPhys,
        (U64)own.PrivateUsage, (U64)own.WorkingSetSize, ft(kernel_time) + ft(user_time),
        focused ? "true" : "false", partial ? "true" : "false");
    if (!emit(buffer)) return 0;
    for (size_t i = 0; i < group_count; ++i) {
        Group *group = groups + i;
        snprintf(buffer, sizeof(buffer), "%s{\"pid\":%lu,\"ppid\":%lu,\"created_100ns\":%llu,"
            "\"root_pid\":%lu,\"root_created_100ns\":%llu,\"name\":\"claude.exe\",\"member_count\":%lu,"
            "\"private_bytes\":%llu,\"working_set_bytes\":%llu,\"cpu_100ns\":%llu}",
            i ? "," : "", (unsigned long)group->pid, (unsigned long)group->parent, group->born,
            (unsigned long)group->pid, group->born, (unsigned long)group->count,
            group->private_bytes, group->working_set, group->cpu);
        if (!emit(buffer)) return 0;
    }
    return emit("]}\n");
}
typedef struct { HANDLE process, job; U64 began; int final; DWORD last_code; } Helper;
static int helper_start(Helper *helper, const wchar_t *python, const wchar_t *report,
                        const wchar_t *directory, const wchar_t *history, const wchar_t *projects, int final) {
    if (!local_directory(directory) || GetFileAttributesW(report) == INVALID_FILE_ATTRIBUTES) return 0;
    wchar_t command[32767], path[PATH_UNITS];
    int length = final
        ? swprintf(command, 32767, L"\"%ls\" \"%ls\" --data \"%ls\" --since-start", python, report, directory)
        : swprintf(command, 32767, L"\"%ls\" \"%ls\" --collect-events --data \"%ls\" --history \"%ls\" --projects \"%ls\" --since-start",
                   python, report, directory, history, projects);
    if (length < 0 || !join(path, directory, L"helper.log")) return 0;
    HANDLE job = CreateJobObjectW(NULL, NULL);
    HANDLE output = INVALID_HANDLE_VALUE, input = INVALID_HANDLE_VALUE;
    STARTUPINFOEXW startup = {0}; PROCESS_INFORMATION process = {0};
    int initialized = 0, ok = 0;
    if (!job) return 0;
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = {0};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_JOB_MEMORY | JOB_OBJECT_LIMIT_ACTIVE_PROCESS;
    limits.JobMemoryLimit = 192 * 1024 * 1024; limits.BasicLimitInformation.ActiveProcessLimit = 1;
    if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits, sizeof(limits))) goto cleanup;
    SECURITY_ATTRIBUTES inherit = {sizeof(inherit), NULL, TRUE};
    input = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &inherit, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    /* Reuse the exact originally-created log handle. Never re-open a path that
       could have become a symlink/hardlink between helper invocations. */
    if (!regular_file(helper_log) || !DuplicateHandle(GetCurrentProcess(), helper_log,
        GetCurrentProcess(), &output, 0, TRUE, DUPLICATE_SAME_ACCESS)) goto cleanup;
    if (input == INVALID_HANDLE_VALUE || output == INVALID_HANDLE_VALUE) goto cleanup;
    LARGE_INTEGER size;
    if (!GetFileSizeEx(output, &size) || size.QuadPart > 1024 * 1024) goto cleanup;
    SIZE_T bytes = 0; InitializeProcThreadAttributeList(NULL, 2, 0, &bytes);
    startup.lpAttributeList = calloc(1, bytes);
    if (!startup.lpAttributeList || !InitializeProcThreadAttributeList(startup.lpAttributeList, 2, 0, &bytes)) goto cleanup;
    initialized = 1;
    HANDLE inherited[2] = {input, output};
    if (!UpdateProcThreadAttribute(startup.lpAttributeList, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                                  inherited, sizeof(inherited), NULL, NULL)
        || !UpdateProcThreadAttribute(startup.lpAttributeList, 0, PROC_THREAD_ATTRIBUTE_JOB_LIST,
                                     &job, sizeof(job), NULL, NULL)) goto cleanup;
    startup.StartupInfo.cb = sizeof(startup);
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = input; startup.StartupInfo.hStdOutput = startup.StartupInfo.hStdError = output;
    if (!CreateProcessW(python, command, NULL, NULL, TRUE,
        CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT, NULL, directory, &startup.StartupInfo, &process)) goto cleanup;
    helper->process = process.hProcess; helper->job = job; helper->began = GetTickCount64(); helper->final = final;
    CloseHandle(process.hThread); ok = 1;
cleanup:
    if (initialized) DeleteProcThreadAttributeList(startup.lpAttributeList);
    free(startup.lpAttributeList);
    if (input != INVALID_HANDLE_VALUE) CloseHandle(input);
    if (output != INVALID_HANDLE_VALUE) CloseHandle(output);
    if (!ok) CloseHandle(job);
    return ok;
}
static int helper_collect(Helper *helper, int force) {
    if (!helper->process) return 0;
    DWORD status = WaitForSingleObject(helper->process, 0);
    if (status == WAIT_TIMEOUT && !force && GetTickCount64() - helper->began < 30000) return 1;
    int clean = 1;
    if (status == WAIT_TIMEOUT || status == WAIT_FAILED) {
        clean = TerminateJobObject(helper->job, 124)
            && WaitForSingleObject(helper->process, 5000) == WAIT_OBJECT_0;
    }
    DWORD code = 70; GetExitCodeProcess(helper->process, &code);
    char buffer[192];
    snprintf(buffer, sizeof(buffer), "{\"type\":\"helper\",\"ts_ms\":%llu,\"final\":%s,\"exit_code\":%lu}\n",
        now_ms(), helper->final ? "true" : "false", (unsigned long)code);
    emit(buffer);
    CloseHandle(helper->job); CloseHandle(helper->process);
    memset(helper, 0, sizeof(*helper));
    helper->last_code = code;
    return clean ? 0 : -1;
}
static void helper_failed(int final) {
    char buffer[192];
    snprintf(buffer, sizeof(buffer), "{\"type\":\"helper\",\"ts_ms\":%llu,\"final\":%s,"
        "\"exit_code\":70,\"start_failed\":true}\n", now_ms(), final ? "true" : "false");
    emit(buffer);
}
static int number(const wchar_t *text, U64 *out) {
    if (!*text) return 0;
    for (const wchar_t *c = text; *c; ++c) if (*c < L'0' || *c > L'9') return 0;
    errno = 0; wchar_t *end = NULL;
    *out = _wcstoui64(text, &end, 10);
    return !errno && end && !*end;
}
int wmain(int argc, wchar_t **argv) {
    const wchar_t *directory = NULL, *python = NULL, *report = NULL, *history = NULL, *projects = NULL;
    U64 deadline = 0, interval = 5000, metadata_interval = 60000;
    for (int i = 1; i + 1 < argc; i += 2) {
        if (wcspbrk(argv[i + 1], L"\"\r\n")) return 64;
        if (!wcscmp(argv[i], L"--output")) directory = argv[i + 1];
        else if (!wcscmp(argv[i], L"--python")) python = argv[i + 1];
        else if (!wcscmp(argv[i], L"--report")) report = argv[i + 1];
        else if (!wcscmp(argv[i], L"--history")) history = argv[i + 1];
        else if (!wcscmp(argv[i], L"--projects")) projects = argv[i + 1];
        else if (!wcscmp(argv[i], L"--deadline-ms")) { if (!number(argv[i + 1], &deadline)) return 64; }
        else if (!wcscmp(argv[i], L"--interval-ms")) { if (!number(argv[i + 1], &interval)) return 64; }
        else if (!wcscmp(argv[i], L"--metadata-ms")) { if (!number(argv[i + 1], &metadata_interval)) return 64; }
        else return 64;
    }
    U64 started = now_ms();
    if (!(argc & 1) || !directory || !python || !report || !history || !projects
        || deadline <= started || deadline - started > 86400000 || interval < 200 || interval > 60000
        || metadata_interval < 1000 || metadata_interval > 3600000) return 64;
    if (!process_user(GetCurrentProcess(), &own_user)) return 70;
    if (!local_directory(directory)) return 73;
    HANDLE directory_handle = CreateFileW(directory, FILE_READ_ATTRIBUTES, FILE_SHARE_READ | FILE_SHARE_WRITE,
        NULL, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, NULL);
    if (directory_handle == INVALID_HANDLE_VALUE) return 73;
    wchar_t path[PATH_UNITS], stop[PATH_UNITS];
    if (!join(path, directory, L"samples.jsonl") || !join(stop, directory, L"stop.request")) { CloseHandle(directory_handle); return 73; }
    log_file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_NEW, FILE_FLAG_OPEN_REPARSE_POINT, NULL);
    if (log_file == INVALID_HANDLE_VALUE || !regular_file(log_file)) {
        if (log_file != INVALID_HANDLE_VALUE) CloseHandle(log_file);
        CloseHandle(directory_handle); return 73;
    }
    if (join(path, directory, L"helper.log"))
        helper_log = CreateFileW(path, FILE_APPEND_DATA | FILE_READ_ATTRIBUTES, FILE_SHARE_READ,
            NULL, CREATE_NEW, FILE_FLAG_OPEN_REPARSE_POINT, NULL);
    if (helper_log == INVALID_HANDLE_VALUE || !regular_file(helper_log)) {
        if (helper_log != INVALID_HANDLE_VALUE) CloseHandle(helper_log);
        CloseHandle(log_file); CloseHandle(directory_handle); return 73;
    }
    nodes = calloc(NODES, sizeof(*nodes)); previous = calloc(NODES, sizeof(*previous));
    if (!nodes || !previous) {
        CloseHandle(helper_log); CloseHandle(log_file); CloseHandle(directory_handle);
        free(nodes); free(previous); return 70;
    }
    char buffer[512];
    snprintf(buffer, sizeof(buffer), "{\"type\":\"start\",\"version\":1,\"interval_ms\":%llu,\"started_unix_ms\":%llu,"
        "\"deadline_unix_ms\":%llu,\"aggregation\":\"root_groups\",\"cpu_scope\":\"observed_member_deltas\"}\n",
        interval, started, deadline);
    int ok = emit(buffer); U64 next_sample = 0, next_metadata = 0, next_flush = 0;
    U64 began = GetTickCount64(); const char *reason = "deadline";
    U64 duration = deadline - started;
    Helper helper = {0};
    while (ok) {
        U64 now = now_ms(), tick = GetTickCount64();
        if (helper_collect(&helper, 0) < 0) { ok = 0; break; }
        if (now >= deadline || tick - began >= duration) break;
        if (GetFileAttributesW(stop) != INVALID_FILE_ATTRIBUTES) { reason = "requested"; break; }
        if (written > LOG_CAP - 32768) { reason = "limit"; break; }
        if (tick >= next_sample) { ok = emit_sample(); next_sample = GetTickCount64() + interval; }
        if (tick >= next_flush) { ok = ok && FlushFileBuffers(log_file); next_flush = tick + 60000; }
        /* Sampling or a slow flush can cross the cutoff. Never start another
           source-reading helper on the strength of the older loop timestamp. */
        if (!ok || now_ms() >= deadline || GetTickCount64() - began >= duration) break;
        if (!helper.process && tick >= next_metadata) {
            if (!helper_start(&helper, python, report, directory, history, projects, 0)) helper_failed(0);
            next_metadata = tick + metadata_interval;
        }
        Sleep(200);
    }
    if (!ok) reason = "error";
    int helper_clean = helper_collect(&helper, 1) >= 0;
    snprintf(buffer, sizeof(buffer), "{\"type\":\"end\",\"ts_ms\":%llu,\"reason\":\"%s\"}\n", now_ms(), reason);
    int end_written = emit(buffer) && FlushFileBuffers(log_file);
    int report_generated = 0;
    DWORD report_code = 70;
    if (helper_clean && helper_start(&helper, python, report, directory, history, projects, 1)) {
        int running;
        while ((running = helper_collect(&helper, 0)) > 0) Sleep(100);
        report_code = helper.last_code;
        report_generated = running == 0 && report_code == 0;
    } else helper_failed(1);
    if (join(path, directory, L"completion.json")) {
        HANDLE completion = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_NEW,
            FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_WRITE_THROUGH, NULL);
        snprintf(buffer, sizeof(buffer), "{\"version\":1,\"sampling_reason\":\"%s\",\"sampling_end_written\":%s,"
            "\"report_generated\":%s,\"report_exit_code\":%lu,\"ended_unix_ms\":%llu}\n",
            reason, end_written ? "true" : "false", report_generated ? "true" : "false",
            (unsigned long)report_code, now_ms());
        DWORD count_written = 0;
        if (completion == INVALID_HANDLE_VALUE || !regular_file(completion)
            || !WriteFile(completion, buffer, (DWORD)strlen(buffer), &count_written, NULL)
            || count_written != strlen(buffer) || !FlushFileBuffers(completion)) ok = 0;
        if (completion != INVALID_HANDLE_VALUE) CloseHandle(completion);
    } else ok = 0;
    FlushFileBuffers(log_file); CloseHandle(log_file); CloseHandle(helper_log);
    CloseHandle(directory_handle);
    if (!end_written || !helper_clean || !report_generated) ok = 0;
    free(nodes); free(previous);
    return ok ? 0 : 70;
}
