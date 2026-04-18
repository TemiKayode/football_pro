"""
bet_executor.py — Automate bet placement on SportyBet via Selenium.

Credentials MUST be set as environment variables:
  SPORTYBET_USERNAME
  SPORTYBET_PASSWORD

Never hardcode credentials in source code.
"""

import os
import time
import random
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from dotenv import load_dotenv

load_dotenv()

LOGIN_URL = "https://www.sportybet.com/ng/"


def kelly_criterion(
    probability: float,
    decimal_odds: float,
    bankroll: float,
    kelly_fraction: float = 0.25,
) -> float:
    """
    Fractional Kelly Criterion stake calculator.

    probability:    model's estimated win probability
    decimal_odds:   bookmaker decimal odds
    bankroll:       current total bankroll
    kelly_fraction: fraction of full Kelly to use (0.25 = quarter Kelly,
                    more conservative and safer for real-money use)
    Returns recommended stake in currency units (0 if no edge).
    """
    b = decimal_odds - 1  # net profit per unit staked
    q = 1 - probability   # probability of losing
    kelly = (b * probability - q) / b
    if kelly <= 0:
        return 0.0
    return round(bankroll * kelly * kelly_fraction, 2)


class BetExecutor:
    def __init__(self, headless: bool = False):
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
        self.wait = WebDriverWait(self.driver, 20)

    def _delay(self, min_s: float = 1.0, max_s: float = 3.0) -> None:
        time.sleep(random.uniform(min_s, max_s))

    def login(self, username: str | None = None, password: str | None = None) -> None:
        username = username or os.environ["SPORTYBET_USERNAME"]
        password = password or os.environ["SPORTYBET_PASSWORD"]

        self.driver.get(LOGIN_URL)
        self._delay()

        login_btn = self.wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "[class*='login'], [data-id='login']"))
        )
        login_btn.click()
        self._delay(0.5, 1.5)

        user_field = self.wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='text'], input[name*='user']"))
        )
        user_field.send_keys(username)
        self._delay(0.3, 0.8)

        pass_field = self.driver.find_element(By.CSS_SELECTOR, "input[type='password']")
        pass_field.send_keys(password)
        self._delay(0.3, 0.8)

        submit = self.driver.find_element(
            By.CSS_SELECTOR, "button[type='submit'], [class*='submit-btn']"
        )
        submit.click()
        self._delay(2, 4)

    def navigate_to_match(self, match_name: str) -> bool:
        """
        Search for a match by name (e.g. "Team A vs Team B").
        Returns True if the match page was found.
        """
        search_url = (
            f"https://www.sportybet.com/ng/sport/football?q={match_name.replace(' ', '+')}"
        )
        self.driver.get(search_url)
        self._delay()
        try:
            self.wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "[class*='match'], [class*='event']")
                )
            )
            return True
        except Exception:
            return False

    def place_bet(self, stake_amount: float) -> bool:
        """
        Assumes the correct odds selection is already highlighted in the betslip.
        Enters the stake and confirms the bet.
        Returns True if bet was placed successfully.
        """
        try:
            stake_input = self.wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "input[class*='stake'], input[placeholder*='Stake']")
                )
            )
            stake_input.clear()
            stake_input.send_keys(str(stake_amount))
            self._delay(0.5, 1.5)

            place_btn = self.wait.until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "[class*='place-bet'], [class*='confirm-bet']")
                )
            )
            place_btn.click()
            self._delay(2, 4)
            print(f"Bet of {stake_amount} placed successfully.")
            return True
        except Exception as e:
            print(f"Bet placement failed: {e}")
            return False

    def close(self) -> None:
        self.driver.quit()


if __name__ == "__main__":
    # Example usage — reads credentials from .env
    stake = kelly_criterion(probability=0.55, decimal_odds=2.10, bankroll=1000, kelly_fraction=0.25)
    print(f"Recommended stake: {stake}")
