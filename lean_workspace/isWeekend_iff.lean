import Mathlib

inductive Day
  | monday
  | tuesday
  | wednesday
  | thursday
  | friday
  | saturday
  | sunday
  deriving DecidableEq

def isWeekend (d : Day) : Bool :=
  match d with
  | Day.saturday => true
  | Day.sunday => true
  | _ => false

theorem isWeekend_iff (d : Day) :
    isWeekend d = true ↔ d = Day.saturday ∨ d = Day.sunday := by
  cases d <;> decide
