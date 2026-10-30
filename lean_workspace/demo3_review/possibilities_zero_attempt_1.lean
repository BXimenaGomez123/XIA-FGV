import Mathlib

def possibilities : ℕ → ℕ
  | 0 => 1
  | n + 1 => 2 * possibilities n

lemma possibilities_zero : possibilities 0 = 1 := rfl

