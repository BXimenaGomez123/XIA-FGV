import Mathlib

def possibilities : ℕ → ℕ
  | 0 => 1
  | n + 1 => 2 * possibilities n

lemma possibilities_zero : possibilities 0 = 1 := rfl

theorem possibilities_pow (n : ℕ) : possibilities n = 2 ^ n := by
  induction n with
  | zero => rfl
  | succ n ih =>
    rw [possibilities, ih, pow_succ, mul_comm]

lemma possibilities_positive (n : ℕ) : 0 < possibilities n := by
  induction n with
  | zero =>
    rw [possibilities]
    decide
  | succ n ih =>
    rw [possibilities]
    omega

theorem possibilities_add (n m : ℕ) : possibilities (n + m) = possibilities n * possibilities m := by
  rw [possibilities_pow, possibilities_pow, possibilities_pow, pow_add]

inductive Day where
  | monday
  | tuesday
  | wednesday
  | thursday
  | friday
  | saturday
  | sunday

def isWeekend : Day → Bool
  | Day.monday => false
  | Day.tuesday => false
  | Day.wednesday => false
  | Day.thursday => false
  | Day.friday => false
  | Day.saturday => true
  | Day.sunday => true

theorem notWeekend_iff (d : Day) : isWeekend d = false ↔ d ≠ Day.saturday ∧ d ≠ Day.sunday := by
  cases d <;> simp

