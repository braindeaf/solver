#!/usr/bin/python3

# https://github.com/google/or-tools/blob/master/examples/python/shift_scheduling_sat.py

# Copyright 2010-2018 Google LLC
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Creates a shift scheduling problem and solves it."""

from __future__ import print_function

import argparse

from ortools.sat.python import cp_model

from google.protobuf import text_format

from yaml import load, dump
try:
    from yaml import CLoader as Loader, CDumper as Dumper
except ImportError:
    from yaml import Loader, Dumper

import math

PARSER = argparse.ArgumentParser()
PARSER.add_argument(
    '--output_proto',
    default="",
    help='Output file to write the cp_model'
    'proto to.')
PARSER.add_argument(
    '--input', default='data-pos.yaml', help="YAML"
)
PARSER.add_argument('--params', default="", help='Sat solver parameters.')


def negated_bounded_span(works, start, length):
    """Filters an isolated sub-sequence of variables assined to True.

  Extract the span of Boolean variables [start, start + length), negate them,
  and if there is variables to the left/right of this span, surround the span by
  them in non negated form.

  Args:
    works: a list of variables to extract the span from.
    start: the start to the span.
    length: the length of the span.

  Returns:
    a list of variables which conjunction will be false if the sub-list is
    assigned to True, and correctly bounded by variables assigned to False,
    or by the start or end of works.
  """
    sequence = []
    # Left border (start of works, or works[start - 1])
    if start > 0:
        sequence.append(works[start - 1])
    for i in range(length):
        sequence.append(works[start + i].Not())
    # Right border (end of works or works[start + length])
    if start + length < len(works):
        sequence.append(works[start + length])
    return sequence


def add_soft_sequence_constraint(model, works, hard_min, soft_min, min_cost,
                                 soft_max, hard_max, max_cost, prefix):
    """Sequence constraint on true variables with soft and hard bounds.

  This constraint look at every maximal contiguous sequence of variables
  assigned to true. If forbids sequence of length < hard_min or > hard_max.
  Then it creates penalty terms if the length is < soft_min or > soft_max.

  Args:
    model: the sequence constraint is built on this model.
    works: a list of Boolean variables.
    hard_min: any sequence of true variables must have a length of at least
      hard_min.
    soft_min: any sequence should have a length of at least soft_min, or a
      linear penalty on the delta will be added to the objective.
    min_cost: the coefficient of the linear penalty if the length is less than
      soft_min.
    soft_max: any sequence should have a length of at most soft_max, or a linear
      penalty on the delta will be added to the objective.
    hard_max: any sequence of true variables must have a length of at most
      hard_max.
    max_cost: the coefficient of the linear penalty if the length is more than
      soft_max.
    prefix: a base name for penalty literals.

  Returns:
    a tuple (variables_list, coefficient_list) containing the different
    penalties created by the sequence constraint.
  """
    cost_literals = []
    cost_coefficients = []

    # Forbid sequences that are too short.
    for length in range(1, hard_min):
        for start in range(len(works) - length + 1):
            model.AddBoolOr(negated_bounded_span(works, start, length))

    # Penalize sequences that are below the soft limit.
    if min_cost > 0:
        for length in range(hard_min, soft_min):
            for start in range(len(works) - length + 1):
                span = negated_bounded_span(works, start, length)
                name = ': under_span(start=%i, length=%i)' % (start, length)
                lit = model.NewBoolVar(prefix + name)
                span.append(lit)
                model.AddBoolOr(span)
                cost_literals.append(lit)
                # We filter exactly the sequence with a short length.
                # The penalty is proportional to the delta with soft_min.
                cost_coefficients.append(min_cost * (soft_min - length))

    # Penalize sequences that are above the soft limit.
    if max_cost > 0:
        for length in range(soft_max + 1, hard_max + 1):
            for start in range(len(works) - length + 1):
                span = negated_bounded_span(works, start, length)
                name = ': over_span(start=%i, length=%i)' % (start, length)
                lit = model.NewBoolVar(prefix + name)
                span.append(lit)
                model.AddBoolOr(span)
                cost_literals.append(lit)
                # Cost paid is max_cost * excess length.
                cost_coefficients.append(max_cost * (length - soft_max))

    # Just forbid any sequence of true variables with length hard_max + 1
    for start in range(len(works) - hard_max):
        model.AddBoolOr(
            [works[i].Not() for i in range(start, start + hard_max + 1)])
    return cost_literals, cost_coefficients


def add_soft_sum_constraint(model, works, hard_min, soft_min, min_cost,
                            soft_max, hard_max, max_cost, prefix):
    """Sum constraint with soft and hard bounds.

  This constraint counts the variables assigned to true from works.
  If forbids sum < hard_min or > hard_max.
  Then it creates penalty terms if the sum is < soft_min or > soft_max.

  Args:
    model: the sequence constraint is built on this model.
    works: a list of Boolean variables.
    hard_min: any sequence of true variables must have a sum of at least
      hard_min.
    soft_min: any sequence should have a sum of at least soft_min, or a linear
      penalty on the delta will be added to the objective.
    min_cost: the coefficient of the linear penalty if the sum is less than
      soft_min.
    soft_max: any sequence should have a sum of at most soft_max, or a linear
      penalty on the delta will be added to the objective.
    hard_max: any sequence of true variables must have a sum of at most
      hard_max.
    max_cost: the coefficient of the linear penalty if the sum is more than
      soft_max.
    prefix: a base name for penalty variables.

  Returns:
    a tuple (variables_list, coefficient_list) containing the different
    penalties created by the sequence constraint.
  """
    cost_variables = []
    cost_coefficients = []
    sum_var = model.NewIntVar(hard_min, hard_max, '')
    # This adds the hard constraints on the sum.
    model.Add(sum_var == sum(works))

    # Penalize sums below the soft_min target.
    if soft_min > hard_min and min_cost > 0:
        delta = model.NewIntVar(-len(works), len(works), '')
        model.Add(delta == soft_min - sum_var)
        # TODO(user): Compare efficiency with only excess >= soft_min - sum_var.
        excess = model.NewIntVar(0, 7, prefix + ': under_sum')
        model.AddMaxEquality(excess, [delta, 0])
        cost_variables.append(excess)
        cost_coefficients.append(min_cost)

    # Penalize sums above the soft_max target.
    if soft_max < hard_max and max_cost > 0:
        delta = model.NewIntVar(-7, 7, '')
        model.Add(delta == sum_var - soft_max)
        excess = model.NewIntVar(0, 7, prefix + ': over_sum')
        model.AddMaxEquality(excess, [delta, 0])
        cost_variables.append(excess)
        cost_coefficients.append(max_cost)

    return cost_variables, cost_coefficients


def solve_shift_scheduling(params, output_proto, input):
    """Solves the shift scheduling problem."""

    data = load(open(input, 'r'), Loader=Loader)
    users = data['users']       # integer
    days = data['days']         # integer
    shifts = ['.', 'D', 'N']    # list

    # Request: (employee, shift, day, weight)
    # A negative weight indicates that the employee desire this assignment.
    requests = [
        # Employee 3 wants the first Saturday off.
        # (3, 0, 5, -2),
        # Employee 5 wants a night shift on the second Thursday.
        # (5, 3, 10, -2),
        # Employee 2 does not want a night shift on the first Friday.
        # (2, 3, 4, 4)
    ]

    # Shift constraints on continuous sequence :
    #     (shift, hard_min, soft_min, min_penalty,
    #             soft_max, hard_max, max_penalty)
    shift_constraints = [
        # betwee 2 consecutive days of night shifts, 1 and 3 are
        # possible but penalized.
        (2, 1, 2, 20, 2, 3, 5),
    ]

    # Penalized transitions:
    #     (previous_shift, next_shift, penalty (0 means forbidden))
    penalized_transitions = [
        # Afternoon to night has a penalty of 4.
        (1, 2, 0),
        # Night to morning is forbidden.
        (2, 1, 0),
    ]

    num_shifts = len(shifts)

    model = cp_model.CpModel()

    work = {}
    for e in range(users):
        for s in range(num_shifts):
            for d in range(days):
                work[e, s, d] = model.NewBoolVar('work%i_%i_%i' % (e, s, d))

    # Linear terms of the objective in a minimization context.
    obj_int_vars = []
    obj_int_coeffs = []
    obj_bool_vars = []
    obj_bool_coeffs = []

    # Each employee has exactly one shift per day.
    for e in range(users):
        for d in range(days):
            model.Add(sum(work[e, s, d] for s in range(num_shifts)) == 1)

    # Exactly one instance of that shift per day
    for d in range(days):
        for s in range(1, num_shifts):
            model.Add(sum(work[(e, s, d)] for e in range(users)) == 1)

    # Employee requests
    for e, s, d, w in requests:
        obj_bool_vars.append(work[e, s, d])
        obj_bool_coeffs.append(w)

    # Shift constraints
    for ct in shift_constraints:
        shift, hard_min, soft_min, min_cost, soft_max, hard_max, max_cost = ct
        for e in range(users):
            works = [work[e, shift, d] for d in range(days)]
            variables, coeffs = add_soft_sequence_constraint(
                model, works, hard_min, soft_min, min_cost, soft_max, hard_max,
                max_cost, 'shift_constraint(employee %i, shift %i)' % (e,
                                                                       shift))
            obj_bool_vars.extend(variables)
            obj_bool_coeffs.extend(coeffs)

    for e in range(users):
        num_shifts_worked = 0
        num_days_worked = 0
        num_nights_worked = 0
        for d in range(days):
            for s in range(1, num_shifts):
                w = work[(e, s, d)]
                num_shifts_worked += w
                if s == 1:
                    num_days_worked += w
                elif s == 2:
                    num_nights_worked += w

        model.Add(num_shifts_worked <= 3)
        model.Add(num_days_worked <= 2)
        model.Add(num_nights_worked <= 2)

    # Penalized transitions
    for previous_shift, next_shift, cost in penalized_transitions:
        for e in range(users):
            for d in range(days - 1):
                transition = [
                    work[e, previous_shift, d].Not(),
                    work[e, next_shift, d + 1].Not()
                ]
                if cost == 0:
                    model.AddBoolOr(transition)
                else:
                    trans_var = model.NewBoolVar(
                        'transition (employee=%i, day=%i)' % (e, d))
                    transition.append(trans_var)
                    model.AddBoolOr(transition)
                    obj_bool_vars.append(trans_var)
                    obj_bool_coeffs.append(cost)

    # Objective
    model.Minimize(
        sum(obj_bool_vars[i] * obj_bool_coeffs[i]
            for i in range(len(obj_bool_vars)))
        + sum(obj_int_vars[i] * obj_int_coeffs[i]
              for i in range(len(obj_int_vars))))

    if output_proto:
        print('Writing proto to %s' % output_proto)
        with open(output_proto, 'w') as text_file:
            text_file.write(str(model))

    # Solve the model.
    solver = cp_model.CpSolver()
    # solver.parameters.max_time_in_seconds = 1000
    solver.parameters.num_search_workers = 8
    if params:
        text_format.Merge(params, solver.parameters)
    solution_printer = cp_model.ObjectiveSolutionPrinter()
    print('Start solving!!!')
    status = solver.SolveWithSolutionCallback(model, solution_printer)

    # Print solution.
    data['status'] = status
    if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
        data['status'] = status
        data['result'] = {}

        for e in range(users):
            for d in range(days):
                for s in range(1, num_shifts):
                    if solver.BooleanValue(work[e, s, d]):
                        data['result'][e] = data['result'].get(e, {})
                        data['result'][e][d] = data['result'][e].get(d, s)
    with open(input, 'w') as file:
        dump(data, file)

def main(args):
    """Main."""
    solve_shift_scheduling(args.params, args.output_proto, args.input)


if __name__ == '__main__':
    main(PARSER.parse_args())
