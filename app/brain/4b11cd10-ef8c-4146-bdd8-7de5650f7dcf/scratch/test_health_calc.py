from app.core.utils import HealthCalculator

def test_health_calc():
    print("Testing HealthCalculator.from_percentage...")
    cases = [
        (0, "good"),
        (50, "good"),
        (69.9, "good"),
        (70, "warning"),
        (85, "warning"),
        (89.9, "warning"),
        (90, "critical"),
        (100, "critical")
    ]
    for pct, expected in cases:
        actual = HealthCalculator.from_percentage(pct)
        print(f"  {pct}% -> {actual} (expected {expected})")
        assert actual == expected

    print("\nTesting HealthCalculator.from_remaining...")
    # limit=100
    assert HealthCalculator.from_remaining(31, 100) == "good"
    assert HealthCalculator.from_remaining(30, 100) == "warning"
    assert HealthCalculator.from_remaining(11, 100) == "warning"
    assert HealthCalculator.from_remaining(10, 100) == "critical"
    assert HealthCalculator.from_remaining(0, 100) == "critical"
    print("  from_remaining OK")

    print("\nTesting HealthCalculator.from_spend...")
    # limit=100
    assert HealthCalculator.from_spend(90, 100) == "good" # $10 left
    assert HealthCalculator.from_spend(95, 100) == "warning" # $5 left
    assert HealthCalculator.from_spend(100, 100) == "critical" # $0 left
    print("  from_spend OK")

    print("\nTesting HealthCalculator.from_balance...")
    assert HealthCalculator.from_balance(10.0) == "good"
    assert HealthCalculator.from_balance(5.0) == "warning"
    assert HealthCalculator.from_balance(4.9) == "warning"
    assert HealthCalculator.from_balance(0.0) == "critical"
    print("  from_balance OK")

if __name__ == "__main__":
    try:
        test_health_calc()
        print("\nAll HealthCalculator tests PASSED!")
    except AssertionError as e:
        print(f"\nTest FAILED: {e}")
