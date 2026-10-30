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

