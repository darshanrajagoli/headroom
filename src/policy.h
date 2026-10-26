#ifndef HEADROOM_POLICY_H
#define HEADROOM_POLICY_H
#include <stddef.h>
#include <stdint.h>
#ifdef _WIN32
#define HR_API __declspec(dllexport)
#else
#define HR_API
#endif
enum { HR_QUEUED = 0, HR_RUNNING = 1, HR_STOPPING = 2, HR_DONE = 3 };
typedef struct {
    uint32_t state, priority, allow_stop, valid;
    uint64_t claim, commit, working_set;
} HrJob;
HR_API int hr_next(const HrJob *jobs, size_t count, uint64_t budget,
                   uint64_t available, uint64_t reserve, int valid);
HR_API int hr_victim(const HrJob *jobs, size_t count, uint64_t total,
                     uint64_t budget, uint64_t available, uint64_t reserve, int valid);
#endif
