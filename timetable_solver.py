"""
University Timetable Scheduler — Tkinter GUI + CP-SAT Solver

The app lets you define rooms, teachers, student groups and subjects,
solves with CP-SAT, shows the timetable grid, and
auto-saves every solution as a timestamped JSON file.
"""

import json
import os
import time
import hashlib
import threading
from datetime import datetime
from collections import defaultdict
from ortools.sat.python import cp_model

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, font as tkfont

# ─────────────────────────── THEME ───────────────────────────
BG        = "#0f1117"
SURFACE   = "#1a1d27"
SURFACE2  = "#242836"
SURFACE3  = "#2e3245"
ACCENT    = "#6c63ff"
ACCENT2   = "#00d4aa"
ACCENT3   = "#ff6584"
TEXT      = "#e8eaf0"
MUTED     = "#7c82a0"
BORDER    = "#2e3245"
SUCCESS   = "#22c55e"
WARNING   = "#f59e0b"
ERROR     = "#ef4444"
WHITE     = "#ffffff"

DAY_COLORS = {
    "Mon": "#6c63ff",
    "Tue": "#00d4aa",
    "Wed": "#f59e0b",
    "Thu": "#ff6584",
    "Fri": "#22c55e",
}

SESSION_COLORS = {
    "theory": "#6c63ff",
    "lab":    "#00d4aa",
}

DAYS = ["Mon","Tue","Wed","Thu","Fri"]
DAY_LABELS = {"Mon":"Monday","Tue":"Tuesday","Wed":"Wednesday","Thu":"Thursday","Fri":"Friday"}
DEFAULT_START_TIME = "08:45"
DEFAULT_END_TIME = "14:15"
DEFAULT_CLASSES_PER_DAY = 8
CURRENT_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PREPOPULATED_DATA_PATH = r"d:\University\7th Semester\AI\TimeTableGenerator\timetable_20260520_014934.json"


def _time_to_minutes(value):
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def _minutes_to_time(total):
    return f"{total // 60:02d}:{total % 60:02d}"


def calculate_classes_per_day(start_time=DEFAULT_START_TIME, end_time=DEFAULT_END_TIME,
                              class_duration_minutes=60, break_after_class=0,
                              break_duration_minutes=0):
    """Calculate how many full classes fit inside the configured day window."""
    total_minutes = _time_to_minutes(end_time) - _time_to_minutes(start_time)
    if total_minutes <= 0 or class_duration_minutes <= 0:
        return 0

    classes_without_break = total_minutes // class_duration_minutes
    if break_after_class <= 0 or break_duration_minutes <= 0:
        return classes_without_break

    classes_with_break = max(0, (total_minutes - break_duration_minutes) // class_duration_minutes)
    return classes_with_break


def build_day_layout(class_duration_minutes=60, break_after_class=0,
                     break_duration_minutes=0, start_time=DEFAULT_START_TIME,
                     end_time=DEFAULT_END_TIME, classes_per_day=None):
    """Return the daily visual layout with class rows and an optional break row."""
    layout = []
    current = _time_to_minutes(start_time)
    break_after_class = int(break_after_class or 0)
    break_duration_minutes = int(break_duration_minutes or 0)
    if classes_per_day is None:
        classes_per_day = calculate_classes_per_day(
            start_time=start_time,
            end_time=end_time,
            class_duration_minutes=class_duration_minutes,
            break_after_class=break_after_class,
            break_duration_minutes=break_duration_minutes,
        )

    for slot_number in range(1, classes_per_day + 1):
        slot_start = current
        slot_end = slot_start + class_duration_minutes
        layout.append({
            "kind": "class",
            "slotNumber": slot_number,
            "start": _minutes_to_time(slot_start),
            "end": _minutes_to_time(slot_end),
        })
        current = slot_end

        if (
            break_after_class > 0
            and break_duration_minutes > 0
            and slot_number == break_after_class
            and slot_number < classes_per_day
        ):
            break_start = current
            break_end = break_start + break_duration_minutes
            layout.append({
                "kind": "break",
                "afterSlotNumber": slot_number,
                "start": _minutes_to_time(break_start),
                "end": _minutes_to_time(break_end),
                "durationMinutes": break_duration_minutes,
                "label": f"Break ({break_duration_minutes} min)",
            })
            current = break_end

    return layout


def build_time_slots(class_duration_minutes=60, break_after_class=0,
                     break_duration_minutes=0, start_time=DEFAULT_START_TIME,
                     end_time=DEFAULT_END_TIME, classes_per_day=None):
    """Build solver time slots and break blocks for the weekly planning horizon."""
    day_layout = build_day_layout(
        class_duration_minutes=class_duration_minutes,
        break_after_class=break_after_class,
        break_duration_minutes=break_duration_minutes,
        start_time=start_time,
        end_time=end_time,
        classes_per_day=classes_per_day,
    )
    time_slots = []
    breaks = []

    for d_idx, day in enumerate(DAYS):
        class_index = 0
        for row in day_layout:
            if row["kind"] == "class":
                class_index += 1
                time_slots.append({
                    "id": f"{day}_{class_index}",
                    "dayId": day,
                    "slotIndex": class_index - 1,
                    "slotNumber": class_index,
                    "start": row["start"],
                    "end": row["end"],
                })
            else:
                breaks.append({
                    "id": f"{day}_break_{row['afterSlotNumber']}",
                    "dayId": day,
                    "afterSlotNumber": row["afterSlotNumber"],
                    "start": row["start"],
                    "end": row["end"],
                    "durationMinutes": row["durationMinutes"],
                    "label": row["label"],
                })

    return time_slots, breaks


def _slot_window_is_contiguous(slot_window, slot_info, class_duration_minutes):
    """Reject windows that jump across configured break periods."""
    if not slot_window:
        return False
    start_min = _time_to_minutes(slot_info[slot_window[0]]["start"])
    end_min = _time_to_minutes(slot_info[slot_window[-1]]["end"])
    return (end_min - start_min) == (len(slot_window) * class_duration_minutes)


def split_lab_blocks(lab_credits):
    """Split lab credits into consecutive same-day blocks of at most 3 slots."""
    remaining = max(0, int(lab_credits or 0))
    blocks = []
    while remaining > 0:
        block = min(3, remaining)
        blocks.append(block)
        remaining -= block
    return blocks


# ─────────────────────────── SOLVER ───────────────────────────

def build_problem(rooms, teachers, groups, offerings, organization_name="My Organization",
                  class_duration_minutes=60, break_after_class=0,
                  break_duration_minutes=0, start_time=DEFAULT_START_TIME,
                  end_time=DEFAULT_END_TIME, classes_per_day=None):
    """Convert GUI data into the internal problem dict."""
    if classes_per_day is None:
        classes_per_day = calculate_classes_per_day(
            start_time=start_time,
            end_time=end_time,
            class_duration_minutes=class_duration_minutes,
            break_after_class=break_after_class,
            break_duration_minutes=break_duration_minutes,
        )
    time_slots, breaks = build_time_slots(
        class_duration_minutes=class_duration_minutes,
        break_after_class=break_after_class,
        break_duration_minutes=break_duration_minutes,
        start_time=start_time,
        end_time=end_time,
        classes_per_day=classes_per_day,
    )
    all_slot_ids = [s["id"] for s in time_slots]

    prob = {
        "metadata": {
            "configId": str(int(time.time()*1000)),
            "configName": f"{organization_name} Timetable",
            "organizationName": organization_name,
            "classDurationMinutes": class_duration_minutes,
            "breakAfterClass": break_after_class,
            "breakDurationMinutes": break_duration_minutes,
            "dayStartTime": start_time,
            "dayEndTime": end_time,
            "classesPerDay": classes_per_day,
        },
        "planningHorizon": {
            "days": [{"id": d, "label": DAY_LABELS[d], "index": i} for i, d in enumerate(DAYS)],
            "slotDurationMinutes": class_duration_minutes,
            "timeSlots": time_slots,
            "blackoutSlots": [],
            "breaks": breaks,
        },
        "resources": {
            "rooms": [
                {
                    "id": r["id"], "name": r["name"],
                    "type": r["type"],  # "lecture" or "lab"
                    "availableSlotIds": all_slot_ids,
                }
                for r in rooms
            ],
            "teachers": [
                {
                    "id": t["id"], "name": t["name"],
                    "maxLoadSlotsPerWeek": int(t.get("max_load", 20)),
                    "maxSessionsPerDay": classes_per_day,
                    "availableSlotIds": all_slot_ids,
                    "blockedSlotIds": [],
                }
                for t in teachers
            ],
            "studentGroups": [
                {
                    "id": g["id"], "name": g["name"],
                    "maxSessionsPerDay": classes_per_day,
                    "availableSlotIds": all_slot_ids,
                    "blockedSlotIds": [],
                }
                for g in groups
            ],
        },
        "courseOfferings": offerings,
        "hardConstraints": {
            "noTeacherOverlap": True, "noRoomOverlap": True,
            "noStudentGroupOverlap": True,
            "respectTeacherAvailability": True,
            "respectRoomAvailability": True,
            "respectStudentGroupAvailability": True,
            "respectRoomTypeAndTags": True,
            "respectTeacherMaxLoad": True,
            "respectGroupMaxSessionsPerDay": True,
            "respectConsecutiveMultiSlotSessions": True,
        },
        "softConstraints": {
            "minimizeTeacherGaps":      {"enabled": True, "weight": 10},
            "minimizeStudentGaps":      {"enabled": True, "weight": 12},
            "spreadSubjectAcrossWeek":  {"enabled": True, "weight": 8},
            "avoidLastSlotAssignments": {"enabled": True, "weight": 4},
        },
        "solverConfig": {
            "timeLimitSeconds": 30,
            "numWorkers": 4,
            "randomSeed": 42,
        },
    }
    return prob


def solve_timetable(prob):
    """Run CP-SAT solver and return result dict."""
    lk = _build_lookups(prob)
    class_duration_minutes = prob["planningHorizon"].get("slotDurationMinutes", 60)
    model = cp_model.CpModel()
    bvars = {}
    session_instances = []
    unschedulable = []

    for offering in prob["courseOfferings"]:
        off_id    = offering["id"]
        batch_id  = offering["batchId"]
        subject_id   = offering["subjectId"]
        subject_name = offering["subjectName"]
        teacher_id   = offering.get("fixedTeacherId") or (
            offering["allowedTeacherIds"][0] if offering["allowedTeacherIds"] else None)

        for session in offering["sessions"]:
            sess_id   = session["id"]
            sess_type = session["type"]
            weekly    = session["weeklyOccurrences"]
            duration  = session["durationSlots"]
            req_type  = session.get("requiredRoomType", "lecture")
            max_pd    = session.get("maxOccurrencesPerDay", 1)

            for inst_idx in range(weekly):
                instance_vars = []
                for room in lk["rooms"]:
                    if room["type"] != req_type:
                        continue
                    room_id = room["id"]
                    for day_id in lk["days"]:
                        day_slots_list = lk["day_slots"].get(day_id, [])
                        n = len(day_slots_list)
                        for start_rank in range(n - duration + 1):
                            window = day_slots_list[start_rank: start_rank + duration]
                            if not _slot_window_is_contiguous(
                                window, lk["slot_info"], class_duration_minutes
                            ):
                                continue
                            if any(s in lk["blackout"] for s in window):
                                continue
                            if not all(s in lk["room_avail"][room_id] for s in window):
                                continue
                            if teacher_id:
                                if not all(
                                    s in lk["teacher_avail"][teacher_id] and
                                    s not in lk["teacher_blocked"][teacher_id]
                                    for s in window
                                ):
                                    continue
                            if not all(
                                s in lk["group_avail"][batch_id] and
                                s not in lk["group_blocked"][batch_id]
                                for s in window
                            ):
                                continue
                            key = (off_id, sess_id, inst_idx, window[0], room_id)
                            var = model.new_bool_var(
                                f"X|{off_id[:8]}|{sess_id[-6:]}|{inst_idx}|{window[0]}|{room_id[-6:]}")
                            bvars[key] = var
                            instance_vars.append({
                                "key": key, "var": var,
                                "day_id": day_id, "slot_window": window,
                                "start_slot_id": window[0], "room_id": room_id,
                                "teacher_id": teacher_id, "batch_id": batch_id,
                                "duration": duration,
                            })

                si = {
                    "off_id": off_id, "sess_id": sess_id, "inst_idx": inst_idx,
                    "sess_type": sess_type, "subject_id": subject_id,
                    "subject_name": subject_name, "teacher_id": teacher_id,
                    "batch_id": batch_id, "weekly_occ": weekly, "duration": duration,
                    "max_per_day": max_pd, "instance_vars": instance_vars,
                    "offering": offering, "session": session,
                }
                session_instances.append(si)
                if not instance_vars:
                    unschedulable.append(si)
                else:
                    model.add_exactly_one([iv["var"] for iv in instance_vars])

    # Overlap maps
    slot_room_vars    = defaultdict(lambda: defaultdict(list))
    slot_teacher_vars = defaultdict(lambda: defaultdict(list))
    slot_batch_vars   = defaultdict(lambda: defaultdict(list))
    for si in session_instances:
        for iv in si["instance_vars"]:
            for slot_id in iv["slot_window"]:
                slot_room_vars[slot_id][iv["room_id"]].append(iv["var"])
                if iv["teacher_id"]:
                    slot_teacher_vars[slot_id][iv["teacher_id"]].append(iv["var"])
                slot_batch_vars[slot_id][iv["batch_id"]].append(iv["var"])

    for slot_id, rm in slot_room_vars.items():
        for r_id, vl in rm.items():
            if len(vl) > 1: model.add_at_most_one(vl)
    for slot_id, tm in slot_teacher_vars.items():
        for t_id, vl in tm.items():
            if len(vl) > 1: model.add_at_most_one(vl)
    for slot_id, bm in slot_batch_vars.items():
        for b_id, vl in bm.items():
            if len(vl) > 1: model.add_at_most_one(vl)

    # Teacher max load
    for teacher in lk["raw_teachers"]:
        t_id = teacher["id"]
        max_load = teacher.get("maxLoadSlotsPerWeek", 999)
        weighted_tvars = [
            iv["duration"] * iv["var"]
            for si in session_instances
            if si["teacher_id"] == t_id
            for iv in si["instance_vars"]
        ]
        if weighted_tvars:
            model.add(sum(weighted_tvars) <= max_load)

    # Lab block grouping: enforce that all lab blocks belonging to the same
    # labBlockGroup (i.e. all lab hours for a course) are scheduled on the
    # same day, in the same room, and occupy a contiguous run of slots.
    lab_groups = defaultdict(list)   # lab_group_id -> list of session_instances
    for si in session_instances:
        if si["sess_type"] != "lab":
            continue
        lg = si["session"].get("labBlockGroup")
        if lg is None:
            continue
        lab_groups[lg].append(si)

    for lg, group_sis in lab_groups.items():
        # number of blocks (sessions) that must be placed for this lab group
        num_blocks = len(group_sis)
        # total number of required slots (sum of durations of the blocks)
        total_required_slots = sum(si["duration"] for si in group_sis)

        # collect all candidate instance-vars belonging to this group
        group_iv_list = []
        for si in group_sis:
            for iv in si["instance_vars"]:
                group_iv_list.append(iv)

        # If no candidate placements were generated, solver will mark unschedulable
        if not group_iv_list:
            continue

        # 1) Force all selected block instances to be on the same day.
        #    y[d] == 1 means this lab group is scheduled on day d.
        y_day = {d: model.new_bool_var(f"LABDAY|{lg[:8]}|{d}") for d in lk["days"]}
        for d in lk["days"]:
            vars_on_day = [iv["var"] for iv in group_iv_list if iv["day_id"] == d]
            if vars_on_day:
                # if any block is placed on day d then y_day[d] must be 1; and
                # if y_day[d]==0 no block can be placed there.
                model.add(sum(vars_on_day) <= num_blocks * y_day[d])
            else:
                # no candidate vars on that day, force y_day[d] to 0
                model.add(y_day[d] == 0)

        # Exactly one day must be chosen for the whole lab group
        model.add(sum(y_day.values()) == 1)

        # 2) Force all selected blocks to be in the same room.
        #    Create z[r] per room used by this group's candidate placements.
        room_ids = sorted({iv["room_id"] for iv in group_iv_list})
        z_room = {r: model.new_bool_var(f"LABROOM|{lg[:8]}|{r[-8:]}") for r in room_ids}
        # link instance-vars to room vars
        for iv in group_iv_list:
            model.add(iv["var"] <= z_room[iv["room_id"]])
        # exactly one room must be chosen
        model.add(sum(z_room.values()) == 1)

        # 3) Enforce contiguous coverage of slots on the chosen day.
        #    For each day, create slot-usage booleans u[d,slot] which are 1 iff
        #    any selected block for this group covers that slot. Then require
        #    that for the chosen day the sum(u) == total_required_slots and
        #    the number of transitions (used->unused or unused->used) <= 2.
        for d in lk["days"]:
            day_slots = lk["day_slots"].get(d, [])
            # create u variables for each slot on this day
            u_vars = [model.new_bool_var(f"LABUSE|{lg[:8]}|{d}|{s}") for s in day_slots]

            # link u_vars with instance-vars that would cover that slot
            for s_idx, s_id in enumerate(day_slots):
                covering = [iv["var"] for iv in group_iv_list if s_id in iv["slot_window"]]
                if covering:
                    # if any covering var is selected then u must be 1, and
                    # if u is 1 at most num_blocks of covering vars can be 1
                    model.add(sum(covering) >= u_vars[s_idx])
                    model.add(sum(covering) <= num_blocks * u_vars[s_idx])
                else:
                    # if no candidate covers this slot, u must be 0
                    model.add(u_vars[s_idx] == 0)

                # u can be 1 only if this day is chosen
                model.add(u_vars[s_idx] <= y_day[d])

            # total used slots on day d must equal required slots if day chosen
            if u_vars:
                model.add(sum(u_vars) == total_required_slots * y_day[d])

                # transitions between adjacent u's; at most 2 when day is chosen
                t_vars = [model.new_bool_var(f"LABTRANS|{lg[:8]}|{d}|{i}") for i in range(len(u_vars)-1)]
                for i in range(len(t_vars)):
                    # t >= u_i - u_{i+1} and t >= u_{i+1} - u_i
                    model.add(t_vars[i] >= u_vars[i] - u_vars[i+1])
                    model.add(t_vars[i] >= u_vars[i+1] - u_vars[i])
                model.add(sum(t_vars) <= 2 * y_day[d])

        # Additionally ensure that total durations assigned to the chosen room
        # equals the required total (this is implied by other constraints but
        # helps solver prune): sum(duration * var) == total_required_slots
        # since each iv.var contributes its block duration when selected.
        model.add(sum(iv["duration"] * iv["var"] for iv in group_iv_list) == total_required_slots)

    # Max per day per session
    osd = defaultdict(list)
    for si in session_instances:
        for iv in si["instance_vars"]:
            osd[(si["off_id"], si["sess_id"], iv["day_id"])].append(iv["var"])
    for (off_id, sess_id, day_id), vl in osd.items():
        mpd = next(
            (si["max_per_day"] for si in session_instances
             if si["off_id"] == off_id and si["sess_id"] == sess_id), 1)
        if len(vl) > mpd:
            model.add(sum(vl) <= mpd)

    # Soft: avoid last slot
    penalty = []
    sc = prob.get("softConstraints", {})
    if sc.get("avoidLastSlotAssignments", {}).get("enabled"):
        w = sc["avoidLastSlotAssignments"]["weight"]
        for si in session_instances:
            for iv in si["instance_vars"]:
                last = lk["day_slots"].get(iv["day_id"], [])[-1:]
                if last and last[0] in iv["slot_window"]:
                    penalty.append((w, iv["var"]))

    if penalty:
        obj = model.new_int_var(0, sum(w for w,_ in penalty)+1, "pen")
        model.add(obj == sum(w*v for w,v in penalty))
        model.minimize(obj)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = prob["solverConfig"].get("timeLimitSeconds", 30)
    solver.parameters.num_workers = prob["solverConfig"].get("numWorkers", 4)
    solver.parameters.random_seed = prob["solverConfig"].get("randomSeed", 42)
    solver.parameters.log_search_progress = False

    wall_start = time.time()
    status = solver.solve(model)
    wall_time = time.time() - wall_start

    status_map = {
        cp_model.OPTIMAL: "OPTIMAL", cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE", cp_model.MODEL_INVALID: "MODEL_INVALID",
        cp_model.UNKNOWN: "UNKNOWN",
    }
    status_str = status_map.get(status, "UNKNOWN")

    meta = prob.get("metadata", {})
    checksum = hashlib.md5(json.dumps(prob, sort_keys=True).encode()).hexdigest()

    result = {
        "schemaVersion": "1.0",
        "exportType": "cp_sat_timetable_solution",
        "metadata": meta,
        "planningHorizon": prob.get("planningHorizon", {}),
        "problemRef": {
            "configId": meta.get("configId",""),
            "configName": meta.get("configName",""),
            "problemChecksum": checksum,
        },
        "solver": {
            "engine": "Google OR-Tools CP-SAT",
            "status": status_str,
            "objectiveValue": solver.objective_value if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None,
            "wallTimeSeconds": round(wall_time, 4),
            "conflicts": solver.num_conflicts,
            "branches": solver.num_branches,
        },
        "assignments": [],
        "unassignedSessions": [],
        "hardConstraintViolations": [],
        "summaries": {"teacherLoad":[], "studentGroupLoad":[], "roomUsage":[]},
    }

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for si in session_instances:
            result["unassignedSessions"].append({
                "sessionId": si["sess_id"], "reason": status_str,
            })
        return result, prob

    t_slots = defaultdict(int)
    b_slots = defaultdict(int)
    r_slots = defaultdict(int)
    ctr = 0

    for si in session_instances:
        assigned = False
        for iv in si["instance_vars"]:
            if solver.value(iv["var"]) == 1:
                assigned = True
                ctr += 1
                w = iv["slot_window"]
                si_info = lk["slot_info"]
                result["assignments"].append({
                    "assignmentId": f"A{ctr:04d}",
                    "courseOfferingId": si["off_id"],
                    "sessionId": si["sess_id"],
                    "sessionInstanceIndex": si["inst_idx"] + 1,
                    "batchId": si["batch_id"],
                    "subjectId": si["subject_id"],
                    "subjectName": si["subject_name"],
                    "teacherId": si["teacher_id"] or "",
                    "roomId": iv["room_id"],
                    "dayId": iv["day_id"],
                    "slotIds": w,
                    "start": si_info[w[0]]["start"],
                    "end": si_info[w[-1]]["end"],
                    "sessionType": si["sess_type"],
                })
                if si["teacher_id"]: t_slots[si["teacher_id"]] += len(w)
                b_slots[si["batch_id"]] += len(w)
                r_slots[iv["room_id"]] += len(w)
                break
        if not assigned:
            result["unassignedSessions"].append({
                "sessionId": si["sess_id"], "instanceIndex": si["inst_idx"],
                "reason": "No variable selected",
            })

    for si in unschedulable:
        result["unassignedSessions"].append({
            "sessionId": si["sess_id"], "reason": "No feasible candidate found",
        })

    for t in lk["raw_teachers"]:
        result["summaries"]["teacherLoad"].append({
            "teacherId": t["id"], "teacherName": t["name"],
            "assignedSlots": t_slots.get(t["id"], 0),
            "maxLoadSlotsPerWeek": t.get("maxLoadSlotsPerWeek", 0),
        })
    for g in lk["raw_groups"]:
        result["summaries"]["studentGroupLoad"].append({
            "batchId": g["id"], "batchName": g["name"],
            "assignedSlots": b_slots.get(g["id"], 0),
        })
    for r in lk["raw_rooms"]:
        result["summaries"]["roomUsage"].append({
            "roomId": r["id"], "roomName": r["name"],
            "usedSlots": r_slots.get(r["id"], 0),
        })

    return result, prob


def _build_lookups(prob):
    horizon = prob["planningHorizon"]
    slot_info = {s["id"]: s for s in horizon["timeSlots"]}
    day_slots = defaultdict(list)
    for s in horizon["timeSlots"]:
        day_slots[s["dayId"]].append(s["id"])
    for d in day_slots:
        day_slots[d].sort(key=lambda sid: slot_info[sid]["slotIndex"])
    days    = [d["id"] for d in horizon["days"]]
    blackout = set(horizon.get("blackoutSlots", []))
    rooms   = prob["resources"]["rooms"]
    teachers= prob["resources"]["teachers"]
    groups  = prob["resources"]["studentGroups"]
    return dict(
        slot_info=slot_info, day_slots=day_slots, days=days, blackout=blackout,
        rooms=rooms, room_avail={r["id"]: set(r.get("availableSlotIds",[])) for r in rooms},
        raw_teachers=teachers, raw_rooms=rooms, raw_groups=groups,
        teacher_avail={t["id"]: set(t.get("availableSlotIds",[])) for t in teachers},
        teacher_blocked={t["id"]: set(t.get("blockedSlotIds",[])) for t in teachers},
        group_avail={g["id"]: set(g.get("availableSlotIds",[])) for g in groups},
        group_blocked={g["id"]: set(g.get("blockedSlotIds",[])) for g in groups},
    )


# ──────────────────────── TKINTER APP ─────────────────────────

def style_widget(w, **kw):
    try: w.configure(**kw)
    except: pass


class ScrollFrame(tk.Frame):
    """A frame with a vertical scrollbar."""
    def __init__(self, parent, **kw):
        super().__init__(parent, bg=kw.get("bg", SURFACE))
        self.canvas = tk.Canvas(self, bg=kw.get("bg", SURFACE),
                                highlightthickness=0, bd=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical",
                                       command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=kw.get("bg", SURFACE))
        self.inner_id = self.canvas.create_window((0,0), window=self.inner,
                                                   anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_inner_configure(self, e):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, e):
        self.canvas.itemconfig(self.inner_id, width=e.width)

    def _on_mousewheel(self, e):
        self.canvas.yview_scroll(int(-1*(e.delta/120)), "units")


class TimetableApp(tk.Tk):
    def __init__(self):
        super().__init__()
        # Use a clean system font for better appearance on Windows
        try:
            self.option_add("*Font", ("Segoe UI", 10))
        except Exception:
            pass
        self.title("AI Timetable Scheduler")
        self.geometry("1300x820")
        self.configure(bg=BG)
        self.resizable(True, True)

        # Data stores
        self.rooms    = []
        self.teachers = []
        self.groups   = []
        self.offerings= []
        self._prepopulated_ids = {
            "rooms": set(),
            "teachers": set(),
            "groups": set(),
            "offerings": set(),
        }
        self._current_results = []
        self._id_counter = 1

        self._setup_styles()
        self._build_ui()
        self._load_defaults()

    def _new_id(self):
        self._id_counter += 1
        return str(int(time.time()*1000) + self._id_counter)

    # ── STYLES ──────────────────────────────────────────────
    def _setup_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook", background=BG, borderwidth=0)
        # slightly larger tab padding for improved click targets
        style.configure(
            "TNotebook.Tab",
            background=SURFACE2,
            foreground=MUTED,
            padding=[18, 10],
            font=("Helvetica", 11, "bold"),
            borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", SURFACE3)],
            foreground=[("selected", TEXT)],
        )

        style.configure(
            "TScrollbar",
            background=SURFACE2,
            troughcolor=SURFACE,
            arrowcolor=MUTED,
            borderwidth=0,
        )

        # make tree rows a bit taller and use the system font for readability
        style.configure(
            "Treeview",
            background=SURFACE2,
            foreground=TEXT,
            fieldbackground=SURFACE2,
            rowheight=34,
            font=("Segoe UI", 10),
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            background=SURFACE3,
            foreground=MUTED,
            font=("Segoe UI", 11, "bold"),
            relief="flat",
            borderwidth=0,
        )
        style.map("Treeview", background=[("selected", ACCENT)],
                  foreground=[("selected", WHITE)])

        style.configure(
            "TCombobox",
            background=SURFACE2,
            foreground=TEXT,
            fieldbackground=SURFACE2,
            selectbackground=ACCENT,
            selectforeground=WHITE,
            arrowcolor=TEXT,
            bordercolor=BORDER,
            lightcolor=SURFACE2,
            darkcolor=SURFACE2,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", SURFACE2), ("disabled", SURFACE2)],
            foreground=[("readonly", TEXT), ("disabled", MUTED)],
            selectbackground=[("readonly", ACCENT)],
            selectforeground=[("readonly", WHITE)],
            arrowcolor=[("readonly", TEXT), ("active", WHITE)],
        )
        self.option_add("*TCombobox*Listbox.background", SURFACE2)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", WHITE)

    # ── UI SKELETON ─────────────────────────────────────────
    def _build_ui(self):
        # Header
        hdr = tk.Frame(self, bg=SURFACE, height=60)
        hdr.pack(fill="x", side="top")
        hdr.pack_propagate(False)
        logo = tk.Label(hdr, text="⚡", bg=ACCENT, fg=WHITE,
                        width=3, font=("Helvetica",18))
        logo.pack(side="left", padx=16, pady=10)
        tk.Label(hdr, text="AI Timetable Scheduler",
                 bg=SURFACE, fg=TEXT, font=("Helvetica",15,"bold")).pack(side="left")
        tk.Label(hdr, text="CP-SAT · OR-Tools",
                 bg=SURFACE, fg=MUTED, font=("Helvetica",10)).pack(side="left", padx=12)

        self.status_lbl = tk.Label(hdr, text="● Ready", bg=SURFACE,
                                   fg=SUCCESS, font=("Helvetica",10,"bold"))
        self.status_lbl.pack(side="right", padx=20)

        # Main paned: left panel + right panel
        paned = tk.PanedWindow(self, orient="horizontal", bg=BG,
                               sashwidth=6, sashrelief="flat",
                               handlesize=0)
        paned.pack(fill="both", expand=True, padx=0, pady=0)

        # Left = input notebook
        left = tk.Frame(paned, bg=BG, width=520)
        paned.add(left, minsize=400)

        # Right = result area
        right = tk.Frame(paned, bg=BG)
        paned.add(right, minsize=500)

        self._build_input_panel(left)
        self._build_result_panel(right)

    # ── INPUT PANEL ─────────────────────────────────────────
    def _build_input_panel(self, parent):
        nb = ttk.Notebook(parent)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self._build_rooms_tab(nb)
        self._build_teachers_tab(nb)
        self._build_groups_tab(nb)
        self._build_subjects_tab(nb)
        self._build_solver_tab(nb)

    def _section(self, parent, title):
        f = tk.Frame(parent, bg=SURFACE, bd=0)
        f.pack(fill="both", expand=True)
        tk.Label(f, text=title, bg=SURFACE, fg=MUTED,
                 font=("Helvetica",10,"bold")).pack(anchor="w", padx=12, pady=(10,4))
        sep = tk.Frame(f, bg=BORDER, height=1)
        sep.pack(fill="x", padx=12, pady=(0,8))
        return f

    def _btn(self, parent, text, cmd, color=ACCENT, fg=WHITE, small=False):
        fs = 9 if small else 11
        b = tk.Button(
            parent,
            text=text,
            command=cmd,
            bg=color,
            fg=fg,
            font=("Segoe UI", fs, "bold"),
            relief="flat",
            bd=0,
            cursor="hand2",
            padx=10 if small else 16,
            pady=4 if small else 7,
            activebackground=ACCENT2,
            activeforeground=WHITE,
        )
        return b

    def _entry_row(self, parent, label, var, row, col=0, width=18, colspan=1):
        tk.Label(parent, text=label, bg=SURFACE2, fg=MUTED,
                 font=("Helvetica",10)).grid(row=row, column=col, sticky="e",
                                              padx=(8,4), pady=4)
        e = tk.Entry(parent, textvariable=var, bg=SURFACE3, fg=TEXT,
                     insertbackground=TEXT, relief="flat", font=("Helvetica",11),
                     width=width, bd=4)
        e.grid(row=row, column=col+1, columnspan=colspan,
               sticky="ew", padx=(0,8), pady=4)
        return e

    def _tree(self, parent, cols, heights=8):
        tv = ttk.Treeview(parent, columns=cols, show="headings", height=heights)
        # use a slightly larger default column width for better readability
        default_width = max(100, 140 // max(1, len(cols)))
        for c in cols:
            tv.heading(c, text=c)
            tv.column(c, width=default_width, anchor="center")
        vsb = ttk.Scrollbar(parent, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.pack(side="left", fill="both", expand=True, padx=(12,0), pady=4)
        vsb.pack(side="left", fill="y", pady=4)
        return tv

    # ── ROOMS TAB ───────────────────────────────────────────
    def _build_rooms_tab(self, nb):
        f = tk.Frame(nb, bg=SURFACE); nb.add(f, text=" 🏛  Rooms ")
        self._section(f, "Add Room")

        form = tk.Frame(f, bg=SURFACE2, bd=0)
        form.pack(fill="x", padx=12, pady=(0,8))
        form.columnconfigure(1, weight=1)

        self._room_name = tk.StringVar(value="r1")
        self._room_type = tk.StringVar(value="lecture")
        self._entry_row(form, "Name:", self._room_name, 0)
        tk.Label(form, text="Type:", bg=SURFACE2, fg=MUTED,
                 font=("Helvetica",10)).grid(row=1, column=0, sticky="e", padx=(8,4), pady=4)
        om = ttk.Combobox(form, textvariable=self._room_type,
                          values=["lecture","lab"], state="readonly", width=14)
        om.grid(row=1, column=1, sticky="ew", padx=(0,8), pady=4)

        btnf = tk.Frame(f, bg=SURFACE)
        btnf.pack(fill="x", padx=12, pady=(0,6))
        self._btn(btnf, "+ Add Room", self._add_room).pack(side="left")
        self._btn(btnf, "✕ Delete", self._del_room, color=SURFACE3, fg=ACCENT3, small=True).pack(side="left", padx=6)

        self._section(f, "Rooms")
        tv_frame = tk.Frame(f, bg=SURFACE); tv_frame.pack(fill="both", expand=True)
        self.rooms_tv = self._tree(tv_frame, ["Name","Type"], heights=6)

    def _add_room(self):
        name = self._room_name.get().strip()
        rtype = self._room_type.get().strip()
        if not name:
            messagebox.showwarning("Input Error", "Room name is required.", parent=self)
            return
        rid = self._new_id()
        self.rooms.append({"id": rid, "name": name, "type": rtype})
        self.rooms_tv.insert("", "end", iid=rid, values=(name, rtype))
        self._room_name.set(f"r{len(self.rooms)+1}")
        self._refresh_combos()

    def _del_room(self):
        sel = self.rooms_tv.selection()
        if not sel: return
        for iid in sel:
            self.rooms = [r for r in self.rooms if r["id"] != iid]
            self.rooms_tv.delete(iid)
        self._refresh_combos()

    # ── TEACHERS TAB ────────────────────────────────────────
    def _build_teachers_tab(self, nb):
        f = tk.Frame(nb, bg=SURFACE); nb.add(f, text=" 👩‍🏫 Teachers ")
        self._section(f, "Add Teacher")

        form = tk.Frame(f, bg=SURFACE2); form.pack(fill="x", padx=12, pady=(0,8))
        form.columnconfigure(1, weight=1)

        self._t_name    = tk.StringVar(value="T1")
        self._t_maxload = tk.StringVar(value="12")
        self._entry_row(form, "Name:",     self._t_name,    0)
        self._entry_row(form, "Max slots/week:", self._t_maxload, 1)

        btnf = tk.Frame(f, bg=SURFACE); btnf.pack(fill="x", padx=12, pady=(0,6))
        self._btn(btnf, "+ Add Teacher", self._add_teacher).pack(side="left")
        self._btn(btnf, "✕ Delete", self._del_teacher, color=SURFACE3, fg=ACCENT3, small=True).pack(side="left", padx=6)

        self._section(f, "Teachers")
        tv_frame = tk.Frame(f, bg=SURFACE); tv_frame.pack(fill="both", expand=True)
        self.teachers_tv = self._tree(tv_frame, ["Name","Max Slots/Wk"], heights=6)

    def _add_teacher(self):
        name = self._t_name.get().strip()
        if not name:
            messagebox.showwarning("Input Error", "Teacher name required.", parent=self)
            return
        try: ml = int(self._t_maxload.get())
        except: ml = 20
        tid = self._new_id()
        self.teachers.append({"id": tid, "name": name, "max_load": ml})
        self.teachers_tv.insert("", "end", iid=tid, values=(name, ml))
        self._t_name.set(f"T{len(self.teachers)+1}")
        self._refresh_combos()

    def _del_teacher(self):
        sel = self.teachers_tv.selection()
        for iid in sel:
            self.teachers = [t for t in self.teachers if t["id"] != iid]
            self.teachers_tv.delete(iid)
        self._refresh_combos()

    # ── GROUPS TAB ──────────────────────────────────────────
    def _build_groups_tab(self, nb):
        f = tk.Frame(nb, bg=SURFACE); nb.add(f, text=" 👥 Groups ")
        self._section(f, "Add Student Group")

        form = tk.Frame(f, bg=SURFACE2); form.pack(fill="x", padx=12, pady=(0,8))
        form.columnconfigure(1, weight=1)

        self._g_name = tk.StringVar(value="SE 7th")
        self._entry_row(form, "Group Name:", self._g_name, 0)

        btnf = tk.Frame(f, bg=SURFACE); btnf.pack(fill="x", padx=12, pady=(0,6))
        self._btn(btnf, "+ Add Group", self._add_group).pack(side="left")
        self._btn(btnf, "✕ Delete", self._del_group, color=SURFACE3, fg=ACCENT3, small=True).pack(side="left", padx=6)

        self._section(f, "Student Groups")
        tv_frame = tk.Frame(f, bg=SURFACE); tv_frame.pack(fill="both", expand=True)
        self.groups_tv = self._tree(tv_frame, ["Group Name"], heights=6)

    def _add_group(self):
        name = self._g_name.get().strip()
        if not name: return
        gid = self._new_id()
        self.groups.append({"id": gid, "name": name})
        self.groups_tv.insert("", "end", iid=gid, values=(name,))
        self._g_name.set(f"Group {len(self.groups)+1}")
        self._refresh_combos()

    def _del_group(self):
        sel = self.groups_tv.selection()
        for iid in sel:
            self.groups = [g for g in self.groups if g["id"] != iid]
            self.groups_tv.delete(iid)
        self._refresh_combos()

    # ── SUBJECTS / OFFERINGS TAB ────────────────────────────
    def _build_subjects_tab(self, nb):
        f = tk.Frame(nb, bg=SURFACE); nb.add(f, text=" 📚 Subjects ")
        self._section(f, "Add Course Offering")

        form = tk.Frame(f, bg=SURFACE2); form.pack(fill="x", padx=12, pady=(0,4))
        form.columnconfigure(1, weight=1); form.columnconfigure(3, weight=1)

        self._s_name     = tk.StringVar(value="Subject1")
        self._s_group    = tk.StringVar()
        self._s_teacher  = tk.StringVar()
        self._s_t_credits= tk.StringVar(value="2")
        self._s_l_credits= tk.StringVar(value="0")

        self._entry_row(form, "Subject Name:", self._s_name,      0, col=0)
        tk.Label(form, text="Group:", bg=SURFACE2, fg=MUTED, font=("Helvetica",10)).grid(
            row=1, column=0, sticky="e", padx=(8,4), pady=4)
        self._s_group_cb = ttk.Combobox(form, textvariable=self._s_group,
                                         state="readonly", width=16)
        self._s_group_cb.grid(row=1, column=1, sticky="ew", padx=(0,8), pady=4)

        tk.Label(form, text="Teacher:", bg=SURFACE2, fg=MUTED, font=("Helvetica",10)).grid(
            row=2, column=0, sticky="e", padx=(8,4), pady=4)
        self._s_teacher_cb = ttk.Combobox(form, textvariable=self._s_teacher,
                                           state="readonly", width=16)
        self._s_teacher_cb.grid(row=2, column=1, sticky="ew", padx=(0,8), pady=4)

        self._entry_row(form, "Theory credits:", self._s_t_credits, 3, col=0)
        self._entry_row(form, "Lab credits:",    self._s_l_credits, 4, col=0)

        btnf = tk.Frame(f, bg=SURFACE); btnf.pack(fill="x", padx=12, pady=(0,6))
        self._btn(btnf, "+ Add Subject", self._add_subject).pack(side="left")
        self._btn(btnf, "✕ Delete", self._del_subject, color=SURFACE3, fg=ACCENT3, small=True).pack(side="left", padx=6)

        self._section(f, "Course Offerings")
        tv_frame = tk.Frame(f, bg=SURFACE); tv_frame.pack(fill="both", expand=True)
        self.subjects_tv = self._tree(tv_frame,
            ["Subject","Group","Teacher","Theory","Lab"], heights=5)

    def _add_subject(self):
        name    = self._s_name.get().strip()
        g_name  = self._s_group.get().strip()
        t_name  = self._s_teacher.get().strip()
        try: tc = int(self._s_t_credits.get())
        except: tc = 0
        try: lc = int(self._s_l_credits.get())
        except: lc = 0
        if not name or not g_name or not t_name:
            messagebox.showwarning("Input Error",
                "Subject name, group and teacher are required.", parent=self)
            return
        if tc == 0 and lc == 0:
            messagebox.showwarning("Input Error",
                "Set at least 1 theory or lab credit.", parent=self)
            return

        group   = next((g for g in self.groups   if g["name"] == g_name), None)
        teacher = next((t for t in self.teachers if t["name"] == t_name), None)
        if not group or not teacher:
            messagebox.showwarning("Not Found",
                "Group or teacher not found. Add them first.", parent=self)
            return

        sid  = self._new_id()
        off_id = f"{group['id']}_{sid}"
        sessions = []
        if tc > 0:
            sessions.append({
                "id": f"{off_id}_theory",
                "type": "theory",
                "weeklyOccurrences": tc,
                "durationSlots": 1,
                "requiredRoomType": "lecture",
                "requiredRoomTags": [],
                "maxOccurrencesPerDay": 1,
                "minDaysBetweenOccurrences": 0,
                "consecutive": False,
            })
        for block_index, block_size in enumerate(split_lab_blocks(lc), start=1):
            sessions.append({
                "id": f"{off_id}_lab_{block_index}",
                "type": "lab",
                "weeklyOccurrences": 1,
                "durationSlots": block_size,
                "requiredRoomType": "lab",
                "requiredRoomTags": [],
                "maxOccurrencesPerDay": 1,
                "minDaysBetweenOccurrences": 0,
                "consecutive": True,
                "labBlockIndex": block_index,
                "labBlockGroup": off_id,
            })

        offering = {
            "id": off_id,
            "batchId": group["id"],
            "batchName": group["name"],
            "subjectId": sid,
            "subjectName": name,
            "allowedTeacherIds": [teacher["id"]],
            "fixedTeacherId": teacher["id"],
            "originalCredits": {"theoryCredits": tc, "labCredits": lc},
            "sessions": sessions,
        }
        self.offerings.append(offering)
        self.subjects_tv.insert("", "end", iid=off_id,
                                values=(name, g_name, t_name, tc, lc))
        self._s_name.set(f"Subject{len(self.offerings)+1}")

    def _del_subject(self):
        sel = self.subjects_tv.selection()
        for iid in sel:
            self.offerings = [o for o in self.offerings if o["id"] != iid]
            self.subjects_tv.delete(iid)

    # ── SOLVER CONFIG TAB ───────────────────────────────────
    def _build_solver_tab(self, nb):
        f = tk.Frame(nb, bg=SURFACE); nb.add(f, text=" ⚙  Solver ")
        self._section(f, "Organization & Schedule")

        form = tk.Frame(f, bg=SURFACE2); form.pack(fill="x", padx=12, pady=(0,8))
        form.columnconfigure(1, weight=1)

        self._cfg_org_name   = tk.StringVar(value="My Organization")
        self._cfg_start_time = tk.StringVar(value=DEFAULT_START_TIME)
        self._cfg_end_time   = tk.StringVar(value=DEFAULT_END_TIME)
        self._cfg_class_dur  = tk.StringVar(value="60")
        self._cfg_break_after = tk.StringVar(value="4")
        self._cfg_break_dur  = tk.StringVar(value="30")
        self._cfg_timelimit  = tk.StringVar(value="30")
        self._cfg_workers    = tk.StringVar(value="4")
        self._cfg_seed       = tk.StringVar(value="42")
        self._cfg_solutions  = tk.StringVar(value="1")
        self._cfg_save_dir   = tk.StringVar(value=CURRENT_PROJECT_DIR)

        self._entry_row(form, "Organization:",      self._cfg_org_name,   0)
        self._entry_row(form, "Start time (HH:MM):", self._cfg_start_time, 1)
        self._entry_row(form, "End time (HH:MM):", self._cfg_end_time, 2)
        self._entry_row(form, "Class duration (min):", self._cfg_class_dur, 3)
        self._entry_row(form, "Break after class #:", self._cfg_break_after, 4)
        self._entry_row(form, "Break duration (min):", self._cfg_break_dur, 5)
        self._entry_row(form, "Time limit (sec):",  self._cfg_timelimit, 6)
        self._entry_row(form, "Workers:",           self._cfg_workers,   7)
        self._entry_row(form, "Random seed:",       self._cfg_seed,      8)
        self._entry_row(form, "Solutions to find:", self._cfg_solutions, 9)

        tk.Label(form, text="Save folder:", bg=SURFACE2, fg=MUTED,
                 font=("Helvetica",10)).grid(row=10, column=0, sticky="e", padx=(8,4), pady=4)
        dir_f = tk.Frame(form, bg=SURFACE2)
        dir_f.grid(row=10, column=1, sticky="ew", padx=(0,8), pady=4)
        tk.Entry(dir_f, textvariable=self._cfg_save_dir, bg=SURFACE3, fg=TEXT,
                 insertbackground=TEXT, relief="flat", font=("Helvetica",10),
                 bd=4).pack(side="left", fill="x", expand=True)
        self._btn(dir_f, "Browse", self._browse_dir, small=True).pack(side="left", padx=4)

        tk.Frame(f, bg=BORDER, height=1).pack(fill="x", padx=12, pady=12)
        self._solve_btn = self._btn(f, "▶  Generate Timetable", self._run_solver)
        self._solve_btn.pack(padx=16, pady=8, fill="x")
        self._btn(
            f,
            "🗑 Clear Prepopulated Data",
            self._clear_prepopulated_data,
            color=SURFACE2,
            fg=ACCENT3,
            small=True,
        ).pack(padx=16, pady=(0, 8), fill="x")

        self._progress = ttk.Progressbar(f, mode="indeterminate", length=200)
        self._progress.pack(fill="x", padx=16, pady=(0,8))

    def _browse_dir(self):
        d = filedialog.askdirectory(parent=self)
        if d: self._cfg_save_dir.set(d)

    # ── RESULT PANEL ────────────────────────────────────────
    def _build_result_panel(self, parent):
        top = tk.Frame(parent, bg=BG)
        top.pack(fill="x", padx=8, pady=(8,0))

        tk.Label(top, text="Timetable Results", bg=BG, fg=TEXT,
                 font=("Helvetica",13,"bold")).pack(side="left")

        self._btn(top, "💾 Export JSON", self._export_json,
                  color=SURFACE2, fg=ACCENT2, small=True).pack(side="right", padx=4)
        self._btn(top, "🗑 Clear", self._clear_results,
                  color=SURFACE2, fg=MUTED, small=True).pack(side="right", padx=4)

        # Stats bar
        self.stats_frame = tk.Frame(parent, bg=SURFACE)
        self.stats_frame.pack(fill="x", padx=8, pady=4)

        # Notebook for grid / assignments / summary
        self.result_nb = ttk.Notebook(parent)
        self.result_nb.pack(fill="both", expand=True, padx=8, pady=(0,8))

        # Tab 1: Timetable Grid
        grid_outer = tk.Frame(self.result_nb, bg=SURFACE)
        self.result_nb.add(grid_outer, text=" 📅 Grid View ")
        self.grid_scroll = ScrollFrame(grid_outer, bg=SURFACE)
        self.grid_scroll.pack(fill="both", expand=True)
        self.grid_canvas_frame = self.grid_scroll.inner

        # Tab 2: Assignments list
        asgn_f = tk.Frame(self.result_nb, bg=SURFACE)
        self.result_nb.add(asgn_f, text=" 📋 Assignments ")
        cols = ["ID","Subject","Type","Group","Teacher","Day","Start","End","Room"]
        self.asgn_tv = self._tree(asgn_f, cols, heights=20)
        for c, w in zip(cols, [55,100,60,80,70,50,60,60,60]):
            self.asgn_tv.column(c, width=w, anchor="center")

        # Tab 3: Summaries
        sum_f = tk.Frame(self.result_nb, bg=SURFACE)
        self.result_nb.add(sum_f, text=" 📊 Summaries ")
        self.sum_text = tk.Text(sum_f, bg=SURFACE2, fg=TEXT,
                                font=("Courier",11), relief="flat",
                                state="disabled", wrap="none")
        self.sum_text.pack(fill="both", expand=True, padx=12, pady=8)

        # Tab 4: Raw JSON
        json_f = tk.Frame(self.result_nb, bg=SURFACE)
        self.result_nb.add(json_f, text=" { } JSON ")
        self.json_text = tk.Text(json_f, bg=SURFACE2, fg=ACCENT2,
                                 font=("Courier",10), relief="flat",
                                 state="disabled", wrap="none")
        vsb = ttk.Scrollbar(json_f, orient="vertical", command=self.json_text.yview)
        hsb = ttk.Scrollbar(json_f, orient="horizontal", command=self.json_text.xview)
        self.json_text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        self.json_text.pack(fill="both", expand=True, padx=12, pady=8)

        # Tab 5: XLSX Export
        export_f = tk.Frame(self.result_nb, bg=SURFACE)
        self.result_nb.add(export_f, text=" 📤 XLSX Export ")
        self.export_scroll = ScrollFrame(export_f, bg=SURFACE)
        self.export_scroll.pack(fill="both", expand=True)
        self.export_frame = self.export_scroll.inner

        self._current_result = None

    # ── COMBO REFRESH ────────────────────────────────────────
    def _refresh_combos(self):
        gnames = [g["name"] for g in self.groups]
        tnames = [t["name"] for t in self.teachers]
        self._s_group_cb["values"]   = gnames
        self._s_teacher_cb["values"] = tnames
        if gnames and not self._s_group.get():   self._s_group.set(gnames[0])
        if tnames and not self._s_teacher.get(): self._s_teacher.set(tnames[0])

    # ── DEFAULTS ────────────────────────────────────────────
    def _load_defaults(self):
        """Pre-populate the UI from the reference dataset, if available."""
        self._cfg_org_name.set("My Organization")
        self._cfg_start_time.set(DEFAULT_START_TIME)
        self._cfg_end_time.set(DEFAULT_END_TIME)
        self._cfg_class_dur.set("60")
        self._cfg_break_after.set("4")
        self._cfg_break_dur.set("30")
        self._load_reference_defaults(PREPOPULATED_DATA_PATH)

    def _load_reference_defaults(self, file_path):
        if not os.path.exists(file_path):
            return

        try:
            with open(file_path, "r", encoding="utf-8") as fp:
                payload = json.load(fp)
        except Exception:
            return

        teacher_summary = payload.get("summaries", {}).get("teacherLoad", [])
        group_summary = payload.get("summaries", {}).get("studentGroupLoad", [])
        room_summary = payload.get("summaries", {}).get("roomUsage", [])
        assignments = payload.get("assignments", [])

        teacher_map = {}
        for teacher in teacher_summary:
            data = {
                "id": teacher["teacherId"],
                "name": teacher.get("teacherName", teacher["teacherId"]),
                "max_load": int(teacher.get("maxLoadSlotsPerWeek", 40)),
            }
            teacher_map[data["id"]] = data
            self.teachers.append(data)
            self.teachers_tv.insert("", "end", iid=data["id"], values=(data["name"], data["max_load"]))
            self._prepopulated_ids["teachers"].add(data["id"])

        group_map = {}
        for group in group_summary:
            data = {
                "id": group["batchId"],
                "name": group.get("batchName", group["batchId"]),
            }
            group_map[data["id"]] = data
            self.groups.append(data)
            self.groups_tv.insert("", "end", iid=data["id"], values=(data["name"],))
            self._prepopulated_ids["groups"].add(data["id"])

        for room in room_summary:
            room_name = room.get("roomName", room["roomId"])
            room_type = "lab" if room_name.lower().startswith("lab") else "lecture"
            data = {
                "id": room["roomId"],
                "name": room_name,
                "type": room_type,
            }
            self.rooms.append(data)
            self.rooms_tv.insert("", "end", iid=data["id"], values=(data["name"], data["type"]))
            self._prepopulated_ids["rooms"].add(data["id"])

        offerings = {}
        for assignment in assignments:
            off_id = assignment["courseOfferingId"]
            offering = offerings.setdefault(off_id, {
                "id": off_id,
                "batchId": assignment["batchId"],
                "batchName": group_map.get(assignment["batchId"], {}).get("name", assignment["batchId"]),
                "subjectId": assignment["subjectId"],
                "subjectName": assignment["subjectName"],
                "teacherId": assignment.get("teacherId", ""),
                "theoryCredits": 0,
                "labCredits": 0,
            })
            slot_count = len(assignment.get("slotIds", [])) or 1
            if assignment.get("sessionType") == "lab":
                offering["labCredits"] += slot_count
            else:
                offering["theoryCredits"] += slot_count

        for offering in offerings.values():
            teacher_id = offering["teacherId"]
            sessions = []
            theory_credits = int(offering["theoryCredits"])
            lab_credits = int(offering["labCredits"])

            if theory_credits > 0:
                sessions.append({
                    "id": f"{offering['id']}_theory",
                    "type": "theory",
                    "weeklyOccurrences": theory_credits,
                    "durationSlots": 1,
                    "requiredRoomType": "lecture",
                    "requiredRoomTags": [],
                    "maxOccurrencesPerDay": 1,
                    "minDaysBetweenOccurrences": 0,
                    "consecutive": False,
                })

            for block_index, block_size in enumerate(split_lab_blocks(lab_credits), start=1):
                sessions.append({
                    "id": f"{offering['id']}_lab_{block_index}",
                    "type": "lab",
                    "weeklyOccurrences": 1,
                    "durationSlots": block_size,
                    "requiredRoomType": "lab",
                    "requiredRoomTags": [],
                    "maxOccurrencesPerDay": 1,
                    "minDaysBetweenOccurrences": 0,
                    "consecutive": True,
                    "labBlockIndex": block_index,
                    "labBlockGroup": offering["id"],
                })

            course_offering = {
                "id": offering["id"],
                "batchId": offering["batchId"],
                "batchName": offering["batchName"],
                "subjectId": offering["subjectId"],
                "subjectName": offering["subjectName"],
                "allowedTeacherIds": [teacher_id] if teacher_id else [],
                "fixedTeacherId": teacher_id or None,
                "originalCredits": {
                    "theoryCredits": theory_credits,
                    "labCredits": lab_credits,
                },
                "sessions": sessions,
            }
            self.offerings.append(course_offering)
            self._prepopulated_ids["offerings"].add(course_offering["id"])
            teacher_name = teacher_map.get(teacher_id, {}).get("name", teacher_id)
            self.subjects_tv.insert(
                "",
                "end",
                iid=course_offering["id"],
                values=(
                    course_offering["subjectName"],
                    course_offering["batchName"],
                    teacher_name,
                    theory_credits,
                    lab_credits,
                ),
            )

        self._refresh_combos()
        if self.groups:
            self._s_group.set(self.groups[0]["name"])
        if self.teachers:
            self._s_teacher.set(self.teachers[0]["name"])
        self._s_name.set("")
        self._s_t_credits.set("0")
        self._s_l_credits.set("0")

    def _clear_prepopulated_data(self):
        total_items = sum(len(ids) for ids in self._prepopulated_ids.values())
        if total_items == 0:
            messagebox.showinfo(
                "Nothing to delete",
                "There is no prepopulated data left to remove.",
                parent=self,
            )
            return

        confirmed = messagebox.askyesno(
            "Delete Prepopulated Data",
            "This will remove all rooms, teachers, groups, and subjects loaded "
            "from the reference JSON file.\n\nContinue?",
            parent=self,
        )
        if not confirmed:
            return

        self.rooms = [
            room for room in self.rooms
            if room["id"] not in self._prepopulated_ids["rooms"]
        ]
        for iid in list(self._prepopulated_ids["rooms"]):
            if self.rooms_tv.exists(iid):
                self.rooms_tv.delete(iid)

        self.teachers = [
            teacher for teacher in self.teachers
            if teacher["id"] not in self._prepopulated_ids["teachers"]
        ]
        for iid in list(self._prepopulated_ids["teachers"]):
            if self.teachers_tv.exists(iid):
                self.teachers_tv.delete(iid)

        self.groups = [
            group for group in self.groups
            if group["id"] not in self._prepopulated_ids["groups"]
        ]
        for iid in list(self._prepopulated_ids["groups"]):
            if self.groups_tv.exists(iid):
                self.groups_tv.delete(iid)

        self.offerings = [
            offering for offering in self.offerings
            if offering["id"] not in self._prepopulated_ids["offerings"]
        ]
        for iid in list(self._prepopulated_ids["offerings"]):
            if self.subjects_tv.exists(iid):
                self.subjects_tv.delete(iid)

        self._prepopulated_ids = {
            "rooms": set(),
            "teachers": set(),
            "groups": set(),
            "offerings": set(),
        }
        self._refresh_combos()
        self._s_group.set("")
        self._s_teacher.set("")
        self._s_name.set("")
        self._s_t_credits.set("0")
        self._s_l_credits.set("0")
        self._clear_results()
        self._set_status("● Prepopulated data cleared", MUTED)

    # ── SOLVER RUNNER ────────────────────────────────────────
    def _run_solver(self):
        if not self.rooms:
            messagebox.showwarning("Missing Data", "Add at least one room.", parent=self); return
        if not self.teachers:
            messagebox.showwarning("Missing Data", "Add at least one teacher.", parent=self); return
        if not self.groups:
            messagebox.showwarning("Missing Data", "Add at least one student group.", parent=self); return
        if not self.offerings:
            messagebox.showwarning("Missing Data", "Add at least one subject.", parent=self); return

        try: n_solutions = max(1, int(self._cfg_solutions.get()))
        except: n_solutions = 1
        try:
            class_duration = max(1, int(self._cfg_class_dur.get()))
        except:
            messagebox.showwarning("Invalid Input", "Class duration must be a positive integer.", parent=self)
            return
        start_time = self._cfg_start_time.get().strip()
        end_time = self._cfg_end_time.get().strip()
        try:
            _time_to_minutes(start_time)
            _time_to_minutes(end_time)
        except Exception:
            messagebox.showwarning("Invalid Input", "Start time and end time must be in HH:MM format.", parent=self)
            return
        if _time_to_minutes(end_time) <= _time_to_minutes(start_time):
            messagebox.showwarning("Invalid Input", "End time must be later than start time.", parent=self)
            return
        try:
            break_after = int(self._cfg_break_after.get() or "0")
        except:
            messagebox.showwarning("Invalid Input", "Break after class must be an integer.", parent=self)
            return
        try:
            break_duration = max(0, int(self._cfg_break_dur.get() or "0"))
        except:
            messagebox.showwarning("Invalid Input", "Break duration must be 0 or a positive integer.", parent=self)
            return

        classes_per_day = calculate_classes_per_day(
            start_time=start_time,
            end_time=end_time,
            class_duration_minutes=class_duration,
            break_after_class=break_after,
            break_duration_minutes=break_duration,
        )
        if classes_per_day < 1:
            messagebox.showwarning(
                "Invalid Input",
                "The selected start time, end time, class duration, and break settings do not allow any full class slot.",
                parent=self,
            )
            return
        if break_after < 0 or break_after >= classes_per_day:
            messagebox.showwarning(
                "Invalid Input",
                f"Break after class must be between 0 and {classes_per_day - 1}. Use 0 for no break.",
                parent=self,
            )
            return
        if break_after == 0:
            break_duration = 0
        organization_name = self._cfg_org_name.get().strip() or "My Organization"

        self._solve_btn.configure(state="disabled", text="⏳  Solving...")
        self._progress.start(10)
        self._set_status("⚙ Solving...", WARNING)

        def worker():
            results = []
            try:
                cfg = {
                    "timeLimitSeconds": int(self._cfg_timelimit.get()),
                    "numWorkers": int(self._cfg_workers.get()),
                    "randomSeed": int(self._cfg_seed.get()),
                }
                for i in range(n_solutions):
                    cfg2 = dict(cfg, randomSeed=cfg["randomSeed"] + i)
                    prob = build_problem(
                        self.rooms,
                        self.teachers,
                        self.groups,
                        self.offerings,
                        organization_name=organization_name,
                        class_duration_minutes=class_duration,
                        break_after_class=break_after,
                        break_duration_minutes=break_duration,
                        start_time=start_time,
                        end_time=end_time,
                        classes_per_day=classes_per_day,
                    )
                    prob["solverConfig"] = cfg2
                    result, prob_out = solve_timetable(prob)
                    results.append((result, prob_out))

                self.after(0, lambda: self._on_solve_done(results))
            except Exception as ex:
                self.after(0, lambda: self._on_solve_error(str(ex)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_solve_done(self, results):
        self._progress.stop()
        self._solve_btn.configure(state="normal", text="▶  Generate Timetable")
        self._current_results = [
            {"solution": result, "problem": prob}
            for result, prob in results
        ]

        # Show first result in UI
        result, prob = results[0]
        status = result["solver"]["status"]

        if status in ("OPTIMAL", "FEASIBLE"):
            self._set_status(f"● {status} — {len(result['assignments'])} assignments", SUCCESS)
            self._render_result(result)
            self._render_export_tab()
            # Auto-save all solutions
            self._auto_save(results)
        else:
            self._set_status(f"● {status}", ERROR)
            messagebox.showerror("Solver Failed",
                f"Status: {status}\n\nCheck that:\n"
                "• You have a lecture room and lab room\n"
                "• Teacher max load is sufficient\n"
                "• All required resources exist", parent=self)
            self._render_result(result)
            self._render_export_tab()

    def _on_solve_error(self, msg):
        self._progress.stop()
        self._solve_btn.configure(state="normal", text="▶  Generate Timetable")
        self._set_status("● Error", ERROR)
        messagebox.showerror("Error", msg, parent=self)

    def _set_status(self, text, color=MUTED):
        self.status_lbl.configure(text=text, fg=color)

    # ── AUTO-SAVE ─────────────────────────────────────────────
    def _auto_save(self, results):
        save_dir = self._cfg_save_dir.get().strip()
        os.makedirs(save_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        saved = []
        for i, (result, prob) in enumerate(results):
            fname = os.path.join(save_dir, f"timetable_{stamp}_v{i+1}.json")
            payload = {"problem": prob, "solution": result}
            with open(fname, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, indent=2, ensure_ascii=False)
            saved.append(fname)
        messagebox.showinfo("Saved",
            f"✅  {len(saved)} solution(s) saved to:\n{save_dir}", parent=self)

    def _export_json(self):
        if not self._current_result:
            messagebox.showinfo("Nothing to export", "Run the solver first.", parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".json",
            filetypes=[("JSON","*.json")],
            initialfile=f"timetable_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._current_result, f, indent=2, ensure_ascii=False)
            messagebox.showinfo("Exported", f"Saved to:\n{path}", parent=self)

    def _clear_results(self):
        self._current_result = None
        self._current_results = []
        for widget in self.grid_canvas_frame.winfo_children():
            widget.destroy()
        for row in self.asgn_tv.get_children():
            self.asgn_tv.delete(row)
        self._set_text(self.sum_text, "")
        self._set_text(self.json_text, "")
        for widget in self.export_frame.winfo_children():
            widget.destroy()
        for w in self.stats_frame.winfo_children():
            w.destroy()

    # ── RENDER RESULT ────────────────────────────────────────
    def _render_result(self, result):
        self._current_result = result
        self._render_stats(result)
        self._render_grid(result)
        self._render_assignments(result)
        self._render_summaries(result)
        self._render_json(result)
        self.result_nb.select(0)

    def _render_export_tab(self):
        for widget in self.export_frame.winfo_children():
            widget.destroy()

        if not self._current_results:
            tk.Label(
                self.export_frame,
                text="No solutions available for XLSX export.",
                bg=SURFACE,
                fg=MUTED,
                font=("Helvetica", 12),
            ).pack(pady=24)
            return

        tk.Label(
            self.export_frame,
            text="Export any found solution to XLSX",
            bg=SURFACE,
            fg=TEXT,
            font=("Helvetica", 12, "bold"),
        ).pack(anchor="w", padx=12, pady=(12, 8))

        for index, payload in enumerate(self._current_results, start=1):
            result = payload["solution"]
            row = tk.Frame(self.export_frame, bg=SURFACE2)
            row.pack(fill="x", padx=12, pady=6)

            details = tk.Frame(row, bg=SURFACE2)
            details.pack(side="left", fill="x", expand=True, padx=12, pady=10)
            tk.Label(
                details,
                text=f"Solution {index}",
                bg=SURFACE2,
                fg=TEXT,
                font=("Helvetica", 11, "bold"),
            ).pack(anchor="w")
            tk.Label(
                details,
                text=(
                    f"Status: {result['solver']['status']}   "
                    f"Assignments: {len(result.get('assignments', []))}   "
                    f"Time: {result['solver'].get('wallTimeSeconds', 0):.2f}s"
                ),
                bg=SURFACE2,
                fg=MUTED,
                font=("Helvetica", 10),
            ).pack(anchor="w", pady=(4, 0))

            self._btn(
                row,
                "Export XLSX",
                lambda idx=index - 1: self._export_solution_to_xlsx(idx),
                color=ACCENT2,
                fg=WHITE,
                small=True,
            ).pack(side="right", padx=12, pady=10)

    def _export_solution_to_xlsx(self, solution_index):
        if solution_index < 0 or solution_index >= len(self._current_results):
            messagebox.showerror("Export Error", "Selected solution is no longer available.", parent=self)
            return

        payload = self._current_results[solution_index]
        result = payload["solution"]
        problem = payload["problem"]
        metadata = problem.get("metadata", {})
        initial_name = (
            f"timetable_solution_{solution_index + 1}_"
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        )
        output_path = filedialog.asksaveasfilename(
            parent=self,
            title="Save XLSX Timetable",
            defaultextension=".xlsx",
            filetypes=[("Excel Workbook", "*.xlsx")],
            initialdir=CURRENT_PROJECT_DIR,
            initialfile=initial_name,
        )
        if not output_path:
            return

        try:
            from timetable_converter import json_to_timetable

            json_to_timetable(
                {"problem": problem, "solution": result},
                output_path=output_path,
                department=metadata.get("organizationName", "Timetable"),
                program="CP-SAT Export",
                semester_label=result.get("problemRef", {}).get("configName", ""),
            )
        except Exception as ex:
            messagebox.showerror("XLSX Export Error", str(ex), parent=self)
            return

        messagebox.showinfo("XLSX Exported", f"Saved to:\n{output_path}", parent=self)

    def _render_stats(self, result):
        for w in self.stats_frame.winfo_children():
            w.destroy()
        meta = result.get("metadata", {})
        stats = [
            ("Organization", meta.get("organizationName", "My Organization")),
            ("Status",    result["solver"]["status"]),
            ("Assignments", str(len(result["assignments"]))),
            ("Unscheduled", str(len(result["unassignedSessions"]))),
            ("Day Window", f"{meta.get('dayStartTime', DEFAULT_START_TIME)} - {meta.get('dayEndTime', DEFAULT_END_TIME)}"),
            ("Class Duration", f"{meta.get('classDurationMinutes', 60)} min"),
            ("Solve time",  f"{result['solver']['wallTimeSeconds']:.2f}s"),
            ("Conflicts",   str(result["solver"]["conflicts"])),
        ]
        for label, val in stats:
            card = tk.Frame(self.stats_frame, bg=SURFACE2,
                            bd=0, relief="flat")
            card.pack(side="left", padx=4, pady=6, ipadx=10, ipady=6)
            tk.Label(card, text=label, bg=SURFACE2, fg=MUTED,
                     font=("Helvetica",9)).pack()
            col = SUCCESS if val in ("OPTIMAL","FEASIBLE") else (
                  ERROR if val in ("INFEASIBLE","MODEL_INVALID") else TEXT)
            tk.Label(card, text=val, bg=SURFACE2, fg=col,
                     font=("Helvetica",13,"bold")).pack()

    def _render_grid(self, result):
        for widget in self.grid_canvas_frame.winfo_children():
            widget.destroy()

        assignments = result.get("assignments", [])
        meta = result.get("metadata", {})
        layout_rows = build_day_layout(
            class_duration_minutes=meta.get("classDurationMinutes", 60),
            break_after_class=meta.get("breakAfterClass", 0),
            break_duration_minutes=meta.get("breakDurationMinutes", 0),
            start_time=meta.get("dayStartTime", DEFAULT_START_TIME),
            end_time=meta.get("dayEndTime", DEFAULT_END_TIME),
            classes_per_day=meta.get("classesPerDay", DEFAULT_CLASSES_PER_DAY),
        )

        if not assignments:
            info = [
                "No assignments to display.",
                f"Organization: {meta.get('organizationName', 'My Organization')}",
                f"Day Window: {meta.get('dayStartTime', DEFAULT_START_TIME)} - {meta.get('dayEndTime', DEFAULT_END_TIME)}",
                f"Class Duration: {meta.get('classDurationMinutes', 60)} min",
            ]
            if meta.get("breakAfterClass", 0) and meta.get("breakDurationMinutes", 0):
                info.append(
                    f"Break: after class {meta['breakAfterClass']} for {meta['breakDurationMinutes']} min"
                )
            tk.Label(self.grid_canvas_frame, text="\n".join(info),
                     bg=SURFACE, fg=MUTED, font=("Helvetica",12)).pack(pady=40)
            return

        batches = sorted(set(a["batchId"] for a in assignments))

        for batch_id in batches:
            batch_name = next(
                (a["batchId"] for a in assignments if a["batchId"] == batch_id),
                batch_id)
            # Find batch name from group list
            g = next((g for g in self.groups if g["id"] == batch_id), None)
            bname = g["name"] if g else batch_id

            tk.Label(self.grid_canvas_frame, text=f"📚  {bname}",
                     bg=SURFACE, fg=TEXT, font=("Helvetica",12,"bold"),
                     anchor="w").pack(fill="x", padx=12, pady=(14,4))

            # Build grid canvas
            cell_w, cell_h = 130, 52
            break_h = 24
            hdr_w, hdr_h   = 90, 30
            cols = len(DAYS)
            row_heights = [break_h if row["kind"] == "break" else cell_h for row in layout_rows]
            canvas_w = hdr_w + cols * cell_w + 10
            canvas_h = hdr_h + sum(row_heights) + 10

            cv = tk.Canvas(self.grid_canvas_frame, bg=SURFACE2,
                           width=canvas_w, height=canvas_h,
                           highlightthickness=0, bd=0)
            cv.pack(padx=12, pady=(0,8), anchor="w")

            # Day headers
            for ci, day in enumerate(DAYS):
                x0 = hdr_w + ci * cell_w
                cv.create_rectangle(x0, 0, x0+cell_w, hdr_h,
                                    fill=DAY_COLORS[day], outline="")
                cv.create_text(x0 + cell_w//2, hdr_h//2,
                               text=DAY_LABELS[day], fill=WHITE,
                               font=("Helvetica",10,"bold"))

            # Time headers + grid lines
            slot_rects = {}
            y_cursor = hdr_h
            for row in layout_rows:
                row_h = break_h if row["kind"] == "break" else cell_h
                cv.create_rectangle(0, y_cursor, hdr_w, y_cursor + row_h,
                                    fill=SURFACE3, outline=BORDER)
                cv.create_text(
                    hdr_w // 2,
                    y_cursor + row_h // 2,
                    text=f"{row['start']}\n{row['end']}" if row["kind"] == "break" else row["start"],
                    fill=MUTED,
                    font=("Helvetica", 8 if row["kind"] == "break" else 9),
                )
                for ci, day in enumerate(DAYS):
                    x0 = hdr_w + ci * cell_w
                    fill = SURFACE3 if row["kind"] == "break" else SURFACE2
                    cv.create_rectangle(
                        x0, y_cursor, x0 + cell_w, y_cursor + row_h,
                        fill=fill, outline=BORDER, width=0.5,
                    )
                    if row["kind"] == "break":
                        cv.create_text(
                            x0 + cell_w // 2,
                            y_cursor + row_h // 2,
                            text=row["label"],
                            fill=WARNING,
                            font=("Helvetica", 9, "bold"),
                        )
                    else:
                        slot_rects[f"{day}_{row['slotNumber']}"] = (
                            y_cursor + 2,
                            y_cursor + row_h - 2,
                        )
                y_cursor += row_h

            # Place assignments
            batch_asgns = [a for a in assignments if a["batchId"] == batch_id]

            for a in batch_asgns:
                day_id = a["dayId"]
                if day_id not in DAYS: continue
                ci = DAYS.index(day_id)
                first_slot = a["slotIds"][0]
                last_slot = a["slotIds"][-1]
                if first_slot not in slot_rects or last_slot not in slot_rects:
                    continue

                x0 = hdr_w + ci * cell_w + 2
                y0 = slot_rects[first_slot][0]
                x1 = x0 + cell_w - 4
                y1 = slot_rects[last_slot][1]

                color = SESSION_COLORS.get(a["sessionType"], ACCENT)
                # Find teacher name
                t = next((t for t in self.teachers if t["id"] == a.get("teacherId","")), None)
                t_name = t["name"] if t else ""
                # Find room name
                r = next((r for r in self.rooms if r["id"] == a.get("roomId","")), None)
                r_name = r["name"] if r else ""

                cv.create_rectangle(x0, y0, x1, y1, fill=color,
                                    outline="", stipple="")
                # Dim overlay for readability
                cv.create_rectangle(x0, y0, x1, y0+14, fill=color, outline="")
                cv.create_text(x0+4, y0+7, anchor="w",
                               text=f"{a['sessionType'].upper()}",
                               fill=WHITE, font=("Helvetica",7,"bold"))
                cv.create_text((x0+x1)//2, (y0+y1)//2 - 8,
                               text=a["subjectName"][:14],
                               fill=WHITE, font=("Helvetica",9,"bold"))
                cv.create_text((x0+x1)//2, (y0+y1)//2 + 6,
                               text=t_name[:12],
                               fill=WHITE, font=("Helvetica",8))
                cv.create_text((x0+x1)//2, (y0+y1)//2 + 18,
                               text=r_name[:10],
                               fill=WHITE, font=("Helvetica",8))

    def _render_assignments(self, result):
        for row in self.asgn_tv.get_children():
            self.asgn_tv.delete(row)
        for a in result.get("assignments", []):
            t = next((t for t in self.teachers if t["id"] == a.get("teacherId","")), None)
            r = next((r for r in self.rooms if r["id"] == a.get("roomId","")), None)
            self.asgn_tv.insert("", "end", values=(
                a["assignmentId"],
                a["subjectName"],
                a["sessionType"],
                a["batchId"][:8],
                t["name"] if t else a.get("teacherId","")[:6],
                a["dayId"],
                a["start"],
                a["end"],
                r["name"] if r else a.get("roomId","")[:6],
            ))

    def _render_summaries(self, result):
        meta = result.get("metadata", {})
        lines = [
            "─" * 50,
            "SCHEDULE SETTINGS",
            "─" * 50,
            f"Organization: {meta.get('organizationName', 'My Organization')}",
            f"Class Duration: {meta.get('classDurationMinutes', 60)} min",
            f"Day Start: {meta.get('dayStartTime', DEFAULT_START_TIME)}",
            f"Day End: {meta.get('dayEndTime', DEFAULT_END_TIME)}",
        ]
        if meta.get("breakAfterClass", 0) and meta.get("breakDurationMinutes", 0):
            lines.append(
                f"Break: after class {meta['breakAfterClass']} for {meta['breakDurationMinutes']} min"
            )
        else:
            lines.append("Break: none")
        lines += ["", "─" * 50, "TEACHER LOAD", "─" * 50]
        for tl in result["summaries"].get("teacherLoad", []):
            bar_used = int(tl["assignedSlots"] / max(tl["maxLoadSlotsPerWeek"], 1) * 20)
            bar = "█" * bar_used + "░" * (20 - bar_used)
            lines.append(
                f"{tl.get('teacherName','?'):12s}  {bar}  "
                f"{tl['assignedSlots']:2d}/{tl['maxLoadSlotsPerWeek']} slots")
        lines += ["", "─" * 50, "ROOM USAGE", "─" * 50]
        for ru in result["summaries"].get("roomUsage", []):
            lines.append(f"{ru.get('roomName','?'):12s}  {ru['usedSlots']:2d} slots used")
        lines += ["", "─" * 50, "GROUP LOAD", "─" * 50]
        for gl in result["summaries"].get("studentGroupLoad", []):
            lines.append(f"{gl.get('batchName','?'):12s}  {gl['assignedSlots']:2d} slots/week")
        if result.get("unassignedSessions"):
            lines += ["", "─" * 50, "⚠  UNASSIGNED SESSIONS", "─" * 50]
            for u in result["unassignedSessions"]:
                lines.append(f"  • {u.get('sessionId','?')} — {u.get('reason','')}")
        self._set_text(self.sum_text, "\n".join(lines))

    def _render_json(self, result):
        self._set_text(self.json_text, json.dumps(result, indent=2, ensure_ascii=False))

    def _set_text(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")


# ─────────────────────────────── MAIN ─────────────────────────
if __name__ == "__main__":
    app = TimetableApp()
    app.mainloop()
