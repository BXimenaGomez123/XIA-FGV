import Mathlib

inductive Day where
  | monday
  | tuesday
  | wednesday
  | thursday
  | friday
  | saturday
  | sunday

def isWeekend : Day → Bool
  | Day.saturday => true
  | Day.sunday => true
  | Day.monday => false
  | Day.tuesday => false
  | Day.wednesday => false
  | Day.thursday => false
  | Day.friday => false

  dfdsfds
