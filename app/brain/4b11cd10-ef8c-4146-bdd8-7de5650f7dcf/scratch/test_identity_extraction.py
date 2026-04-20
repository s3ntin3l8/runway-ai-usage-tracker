from app.core.utils import IdentityExtractor

def test_extract_best_email():
    print("Testing IdentityExtractor.extract_best_email...")

    # Case 1: Real primary verified
    emails1 = [
        {"email": "real@example.com", "primary": True, "verified": True},
        {"email": "58235613+s3ntin3l8@users.noreply.github.com", "primary": False, "verified": True}
    ]
    assert IdentityExtractor.extract_best_email(emails1) == "real@example.com"
    print("  Case 1 (Real primary verified) OK")

    # Case 2: Noreply primary, Real secondary verified
    emails2 = [
        {"email": "58235613+s3ntin3l8@users.noreply.github.com", "primary": True, "verified": True},
        {"email": "real@example.com", "primary": False, "verified": True}
    ]
    assert IdentityExtractor.extract_best_email(emails2) == "real@example.com"
    print("  Case 2 (Noreply primary, Real secondary verified) OK")

    # Case 3: Noreply primary, Real secondary NOT verified
    emails3 = [
        {"email": "58235613+s3ntin3l8@users.noreply.github.com", "primary": True, "verified": True},
        {"email": "unverified@example.com", "primary": False, "verified": False}
    ]
    # Should fallback to primary (noreply) because secondary is not verified
    assert IdentityExtractor.extract_best_email(emails3) == "58235613+s3ntin3l8@users.noreply.github.com"
    print("  Case 3 (Noreply primary, Real secondary NOT verified) OK")

    # Case 4: Only noreply
    emails4 = [
        {"email": "58235613+s3ntin3l8@users.noreply.github.com", "primary": True, "verified": True}
    ]
    assert IdentityExtractor.extract_best_email(emails4) == "58235613+s3ntin3l8@users.noreply.github.com"
    print("  Case 4 (Only noreply) OK")

    # Case 5: Empty list
    assert IdentityExtractor.extract_best_email([]) is None
    print("  Case 5 (Empty list) OK")

if __name__ == "__main__":
    try:
        test_extract_best_email()
        print("\nAll IdentityExtractor tests PASSED!")
    except AssertionError as e:
        print(f"\nTest FAILED: {e}")
