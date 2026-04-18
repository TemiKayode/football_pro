import time
import random
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup


class SportyBetScraper:
    BASE_URL = "https://www.sportybet.com/ng/sport/football"

    def __init__(self, headless: bool = True):
        options = Options()
        if headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=options)
        self.driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

    def _random_delay(self, min_s: float = 1.5, max_s: float = 4.0) -> None:
        time.sleep(random.uniform(min_s, max_s))

    def navigate_to_football(self) -> None:
        self.driver.get(self.BASE_URL)
        WebDriverWait(self.driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "[class*='match'], [class*='event']"))
        )
        self._random_delay()

    def extract_matches(self) -> list[dict]:
        self.navigate_to_football()
        soup = BeautifulSoup(self.driver.page_source, "lxml")
        matches = []

        # SportyBet structures vary; these selectors target common patterns.
        # Adjust if the site updates its HTML.
        for row in soup.select("[class*='match-item'], [class*='event-item'], [class*='fixture']"):
            try:
                teams = row.select("[class*='team-name'], [class*='home'], [class*='away']")
                odds_els = row.select("[class*='odd'], [class*='price']")

                if len(teams) < 2 or len(odds_els) < 3:
                    continue

                home_team = teams[0].get_text(strip=True)
                away_team = teams[1].get_text(strip=True)
                home_odds = float(odds_els[0].get_text(strip=True))
                draw_odds = float(odds_els[1].get_text(strip=True))
                away_odds = float(odds_els[2].get_text(strip=True))

                matches.append(
                    {
                        "home_team": home_team,
                        "away_team": away_team,
                        "home_odds": home_odds,
                        "draw_odds": draw_odds,
                        "away_odds": away_odds,
                    }
                )
            except (ValueError, IndexError):
                continue

        return matches

    def scrape(self) -> list[dict]:
        try:
            return self.extract_matches()
        finally:
            self.driver.quit()


if __name__ == "__main__":
    scraper = SportyBetScraper(headless=True)
    data = scraper.scrape()
    print(f"Found {len(data)} matches")
    for m in data[:5]:
        print(m)
