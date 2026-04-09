from app.core.browser_cookies import get_all_browser_cookies_paths

def test():
    print("Searching for browser cookies...")
    paths = get_all_browser_cookies_paths()
    if not paths:
        print("No browser cookies found.")
        return
    
    for p in paths:
        print(f"Found {p['browser']} ({p['type']}): {p['path']}")

if __name__ == "__main__":
    test()
