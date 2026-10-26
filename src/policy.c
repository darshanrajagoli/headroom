#include "policy.h"
#include <limits.h>

int hr_next(const HrJob *jobs, size_t count, uint64_t budget,
            uint64_t available, uint64_t reserve, int valid) {
    if (!valid || !budget || count > 64) return -1;
    uint64_t reserved = 0, unspent = 0;
    int next = -1;
    for (size_t i = 0; i < count; ++i) {
        const HrJob *j = jobs + i;
        if (j->state == HR_QUEUED && (next < 0 || j->priority > jobs[next].priority))
            next = (int)i;
        if (j->state == HR_RUNNING || j->state == HR_STOPPING) {
            if (!j->valid || j->state == HR_STOPPING) return -1;
            uint64_t held = j->claim > j->commit ? j->claim : j->commit;
            if (held > budget || reserved > budget - held) return -1;
            reserved += held;
            if (j->claim > j->commit) unspent += j->claim - j->commit;
        }
    }
    if (next < 0 || !jobs[next].claim || jobs[next].claim > budget - reserved) return -1;
    if (reserve && (available < reserve || unspent > available - reserve
        || jobs[next].claim > available - reserve - unspent)) return -1;
    return next;
}

int hr_victim(const HrJob *jobs, size_t count, uint64_t total,
              uint64_t budget, uint64_t available, uint64_t reserve, int valid) {
    if (!valid || !budget || count > 64) return -1;
    uint64_t deficit = total > budget ? total - budget : 0;
    uint64_t physical = available < reserve ? reserve - available : 0;
    if (!deficit && !physical) return -1;
    int victim = -1;
    for (size_t i = 0; i < count; ++i) {
        const HrJob *j = jobs + i;
        if (j->state == HR_STOPPING) return -1;
        if ((j->state == HR_RUNNING) && !j->valid) return -1;
        if (j->state != HR_RUNNING || !j->allow_stop || !j->valid || !j->claim
            || j->commit <= j->claim || j->commit < deficit || j->working_set < physical) continue;
        if (victim < 0 || j->priority < jobs[victim].priority
            || (j->priority == jobs[victim].priority
                && j->commit - j->claim > jobs[victim].commit - jobs[victim].claim)) victim = (int)i;
    }
    return victim;
}
