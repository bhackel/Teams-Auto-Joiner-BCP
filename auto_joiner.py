import json
import random
import re
import time
from datetime import datetime
from threading import Timer
import playsound
import ctypes

from selenium import webdriver
from selenium.common import exceptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.keys import Keys
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.utils import ChromeType
from webdriver_manager.microsoft import EdgeChromiumDriverManager
from msedge.selenium_tools import Edge, EdgeOptions

browser: webdriver.Chrome = None
config = None
meetings = []
current_meeting = None
already_joined_ids = []
hangup_thread: Timer = None
join_early_offset = 0

class Meeting:
    def __init__(self, m_id, time_started, title):
        self.m_id = m_id
        self.time_started = time_started
        self.title = title
        self.blacklisted = self.check_blacklist_calendar_meeting()
        self.auto_leave_blacklisted = self.check_blacklist_auto_leave()

    def check_blacklist_calendar_meeting(self):
        if self.title is None:
            return True
        if "blacklist_meeting_re" in config and config['blacklist_meeting_re'] != "":
            regex = config['blacklist_meeting_re']
            return True if re.search(regex, self.title) else False
        return False

    def check_blacklist_auto_leave(self):
        if self.title is None:
            return True
        if "auto_leave_blacklist_re" in config and config['auto_leave_blacklist_re'] != "":
            regex = config['auto_leave_blacklist_re']
            return True if re.search(regex, self.title) else False
        return False

    def __str__(self):
        bl = " [BLACKLISTED]" if self.blacklisted else ""
        joined = " [Already Joined]" if self.m_id in already_joined_ids else ""
        return f"\t{self.title} {self.time_started}{bl}{joined}"


def load_config():
    global config
    with open('config.json') as json_data_file:
        config = json.load(json_data_file)


def init_browser():
    # Setting up the chosen web browser for automation
    global browser

    if "chrome_type" in config and config['chrome_type'] == "msedge":
        chrome_options = EdgeOptions()
        chrome_options.use_chromium = True
    else:
        chrome_options = webdriver.ChromeOptions()

    chrome_options.add_argument('--ignore-certificate-errors')
    chrome_options.add_argument('--ignore-ssl-errors')
    chrome_options.add_argument('--use-fake-ui-for-media-stream')
    chrome_options.add_experimental_option('prefs', {
        'credentials_enable_service': False,
        'profile.default_content_setting_values.media_stream_mic': 1,
        'profile.default_content_setting_values.media_stream_camera': 1,
        'profile.default_content_setting_values.geolocation': 1,
        'profile.default_content_setting_values.notifications': 1,
        'profile': {
            'password_manager_enabled': False
        }
    })
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_experimental_option('excludeSwitches', ['enable-automation', 'enable-logging'])

    if "headless" in config and config['headless']:
        chrome_options.add_argument('--headless')
        print("Enabled headless mode")

    if "mute_audio" in config and config['mute_audio']:
        chrome_options.add_argument('--mute-audio')

    if "chrome_type" in config:
        if config['chrome_type'] == "chromium":
            browser = webdriver.Chrome(ChromeDriverManager(chrome_type=ChromeType.CHROMIUM).install(),
                                       options=chrome_options)
        elif config['chrome_type'] == "msedge":
            browser = Edge(EdgeChromiumDriverManager().install(), options=chrome_options)
        else:
            browser = webdriver.Chrome(ChromeDriverManager().install(), options=chrome_options)
    else:
        browser = webdriver.Chrome(ChromeDriverManager().install(), options=chrome_options)

    # Resize the window according to config, or a minimum
    width = 1200
    if "window_width" in config and config['window_width'] > 0:
        width = config['window_width']
    height = 850
    if "window_height" in config and config['window_height'] > 0:
        height = config['window_height']
    browser.set_window_size(width, height)
    print("Resized window.")


def wait_until_found(sel, timeout, print_error=True):
    # Waits for an element to appear on the page, until timeout
    try:
        element_present = EC.visibility_of_element_located((By.CSS_SELECTOR, sel))
        WebDriverWait(browser, timeout).until(element_present)
        return browser.find_element_by_css_selector(sel)
    except exceptions.TimeoutException:
        if print_error:
            print(f"Timeout waiting for element: {sel}")
        return None


def switch_to_calendar_tab():
    # Clicks the calendar icon on the left of the window
    calendar_button = wait_until_found(
        "button.app-bar-link > ng-include > svg.icons-calendar", 15)
    if calendar_button is not None:
        try:
            calendar_button.click()
            return True
        except:
            print("Failed to open the calendar")
            return False


def prepare_calendar_page():
    # Opens and switches the calendar to Day View so meeting search works
    print("Waiting for calendar to load...")

    # Remove any popups that might block simulated clicks
    try:
        browser.execute_script("document.getElementById('toast-container').remove()")
    except exceptions.JavascriptException:
        pass

    # Open the calendar to switch the view mode
    switch_to_calendar_tab()
    try:
        switcher_string = ".ms-CommandBar-secondaryCommand > div > button[class*='__topBarContent']"
        view_switcher = wait_until_found(switcher_string, 30)

        print("Switching calendar to day view...")

        # Continually try switching to day view
        success = False
        while not success:
            view_switcher = wait_until_found(switcher_string, 1)
            if view_switcher is None:
                print("Reopening calendar page...")
                switch_to_calendar_tab()
                time.sleep(2)
                continue
            # Open switcher and click Day
            browser.execute_script("arguments[0].click();", view_switcher)
            day_button = wait_until_found(
                "li[role='presentation'].ms-ContextualMenu-item>button[aria-posinset='1']", 5)
            day_button.click()
            # Check if the change worked
            view_switcher = wait_until_found(switcher_string, 1)
            if view_switcher is not None and view_switcher.text == "Day":
                success = True
            time.sleep(1)
        print("Switched calendar view mode.")
    except Exception as e:
        print("\nFailed to load calendar:", e)
        exit(1)


def get_calendar_meetings():
    global meetings, join_early_offset

    if wait_until_found("div[class*='__cardHolder']", 5) is None:
        return

    meeting_cards = browser.find_elements_by_css_selector("div[class*='multi-day-renderer__eventCard']")
    if len(meeting_cards) == 0:
        return
    
    try:
        for meeting_card in meeting_cards:
            # Use the card's position on page to find the start time
            style_string = meeting_card.get_attribute("style")
            top_offset = float(style_string[style_string.find("top: ") + 5:style_string.find("rem;")])
            minutes_from_midnight = int(top_offset / .135)
            midnight = datetime.now().replace(hour=0, minute=0, second=0)
            midnight = int(datetime.timestamp(midnight))
            start_time = midnight + minutes_from_midnight * 60

            # Find the meeting duration in seconds using the card height
            card_height_percent = style_string[style_string.find("height: ") + 8:-2]
            duration = round(float(card_height_percent) / 100 * 24 * 60 * 60)
            end_time = start_time + duration

            sec_meeting_card = meeting_card.find_element_by_css_selector("div")
            meeting_name = sec_meeting_card.get_attribute("title").replace("\n", " ")
            meeting_id = sec_meeting_card.get_attribute("id")

            # Check if the current time is within the event card range,
            # then add the meeting to the list
            unix_time = datetime.now().timestamp()
            if unix_time + join_early_offset > start_time and unix_time < end_time:
                meetings.append(Meeting(meeting_id, start_time, meeting_name))
        return True
    except exceptions.StaleElementReferenceException:
        print("Failed to get meeting times.")
        return False

def decide_meeting():
    global meetings

    # Ignore blacklisted and already joined meetings
    meetings = [x for x in meetings if not x.blacklisted]
    meetings = [x for x in meetings if x.m_id not in already_joined_ids]
    if len(meetings) == 0:
        return

    # Sort meetings by closest to current time
    meetings.sort(key=lambda x: x.time_started, reverse=True)
    newest_meeting = meetings[0]

    return newest_meeting


def join_meeting(meeting):
    global hangup_thread, current_meeting, already_joined_ids

    switch_to_calendar_tab()

    # Find the meeting link in the event card edit page
    event_card = wait_until_found(f"div[id='{meeting.m_id}']", 5)
    event_card.click()

    edit_button = wait_until_found('button[class*="meeting-header__button', 1)
    browser.execute_script("arguments[0].click();", edit_button)

    meeting_link = wait_until_found('.me-email-headline', 5)
    if meeting_link:
        url = meeting_link.get_attribute('href')
        # Add /_#/ to the URL to go to the Join Call page
        split_index = url.index('/l/')
        url = url[0:split_index] + "/_#/l/" + url[split_index + 3:]
    else:
        print("\nCould not find meeting link.")
        return

    # Open the meeting link
    browser.get(url)

    join_now_btn = wait_until_found("button[data-tid='prejoin-join-button']", 10)
    if join_now_btn is None:
        return

    # Wait for auto disable by teams
    time.sleep(3)

    # Attempt to turn camera off
    video_btn = browser.find_element_by_css_selector("toggle-button[data-tid='toggle-video']>div>button")
    video_is_on = video_btn.get_attribute("aria-pressed")
    if video_is_on == "true":
        video_btn.click()
        print("Video off")

    # Attempt to turn mic off
    audio_btn = browser.find_element_by_css_selector("toggle-button[data-tid='toggle-mute']>div>button")
    audio_is_on = audio_btn.get_attribute("aria-pressed")
    if audio_is_on == "true":
        audio_btn.click()
        print("Audio off")

    # Wait for a random delay if enabled
    if "random_delay" in config and config['random_delay']:
        delay = random.randrange(10, 31, 1)
        print(f"Waiting for {delay}s")
        time.sleep(delay)

    # Join the meeting. Need to find again to avoid stale element exception
    join_now_btn = wait_until_found("button[data-tid='prejoin-join-button']", 5)
    if join_now_btn is None:
        return
    join_now_btn.click()

    # Play a sound to indicate that the bot has joined a meeting
    if "join_sound" in config and not config["join_sound"] == "":
        try:
            playsound.playsound(config["join_sound"])
            print("Played join sound")
        except playsound.PlaysoundException:
            print("Could not play the specified sound file.")

    current_meeting = meeting
    already_joined_ids.append(meeting.m_id)

    print(f"Joined meeting: {meeting.title}")

    if meeting.auto_leave_blacklisted:
        print("\nMeeting is auto leave blacklisted, will not check member count.\n")

    # Start a thread to hangup the call after delay
    if "auto_leave_after_min" in config and config['auto_leave_after_min'] > 0:
        hangup_thread = Timer(config['auto_leave_after_min'] * 60, hangup)
        hangup_thread.start()


def get_meeting_members():
    # Open the meeting into fullscreen, if it is not already
    meeting_elems = browser.find_elements_by_css_selector('.one-call')
    for meeting_elem in meeting_elems:
        try:
            meeting_elem.click()
            break
        except:
            continue

    # Check if the People list is already open. If not, open it
    try:
        list_closed = False
        ppl_elem = wait_until_found('.people-picker-container', 2)
        ppl_elem = ppl_elem.find_element_by_xpath('../..')
        if "ng-hide" in ppl_elem.get_attribute('class'):
            list_closed = True
    except:
        list_closed = True

    if list_closed:
        print("Participants list is closed, trying to open it...")
        try:
            browser.find_element_by_css_selector("button[id='roster-button']")
            browser.execute_script("document.getElementById('roster-button').click()")
        except:
            return None

    # Use people list to get the number of meeting members
    total_participants = 0

    participants_elem = wait_until_found(
        "calling-roster-section[section-key='participantsInCall'] .roster-list-title", 2)
    attendees_elem = wait_until_found(
        "calling-roster-section[section-key='attendeesInMeeting'] .roster-list-title", 2)

    # Sum the number of users in Participants and Attendees
    try:
        if participants_elem is not None:
            total_participants += sum(
                [int(s) for s in participants_elem.get_attribute("aria-label").split() if s.isdigit()])

        if attendees_elem is not None:
            total_participants += sum(
                [int(s) for s in attendees_elem.get_attribute("aria-label").split() if s.isdigit()])
    except exceptions.StaleElementReferenceException:
        pass

    return total_participants


def hangup():
    global current_meeting
    if current_meeting is None:
        return

    try:
        # Ensure that the disconnect button is loaded by leaving fullscreen
        switch_to_calendar_tab()

        # Hangup the call and reset variables
        hangup_btn = wait_until_found("button[data-tid='call-hangup']", 2)
        hangup_btn.click()
        print(f"Left Meeting: {current_meeting.title}")
        current_meeting = None
        if hangup_thread:
            hangup_thread.cancel()
        return True
    except:
        return False


def main():
    global config, meetings, current_meeting, join_early_offset

    init_browser()

    browser.get("https://teams.microsoft.com")

    # Login to account using email and password
    if config['email'] != "" and config['password'] != "":
        login_email = wait_until_found("input[type='email']", 30)
        if login_email is not None:
            login_email.send_keys(config['email'])

        # find the element again to avoid StaleElementReferenceException
        login_email = wait_until_found("input[type='email']", 5)
        if login_email is not None:
            login_email.send_keys(Keys.ENTER)

        login_pwd = wait_until_found("input[type='password']", 10)
        if login_pwd is not None:
            login_pwd.send_keys(config['password'])

        # find the element again to avoid StaleElementReferenceException
        login_pwd = wait_until_found("input[type='password']", 5)
        if login_pwd is not None:
            login_pwd.send_keys(Keys.ENTER)

        keep_logged_in = wait_until_found("input[id='idBtn_Back']", 5)
        if keep_logged_in is not None:
            keep_logged_in.click()
        else:
            print("Login Unsuccessful, recheck entries in config.json")

        use_web_instead = wait_until_found(".use-app-lnk", 5, print_error=False)
        if use_web_instead is not None:
            use_web_instead.click()

    print("Waiting for correct page...")
    if wait_until_found("#teams-app-bar", 60 * 5) is None:
        exit(1)

    print("Found page.")

    prepare_calendar_page()

    # Delay in seconds between checks for new meetings
    check_interval = 20
    if "check_interval" in config and config['check_interval'] >= 0:
        check_interval = config['check_interval']

    # Delay in seconds between current participant count checks
    member_interval = 10
    if "member_interval" in config and config['member_interval'] >= 0:
        member_interval = config['member_interval']

    # Checks for meeting member count and tries to leave if below threshold
    auto_leave = False
    if "auto_leave" in config and config['auto_leave']:
        auto_leave = True

    # Maximum number of people in meeting to automatically leave
    auto_leave_count = 7
    if "auto_leave_count" in config and config['auto_leave_count'] > 1:
        auto_leave_count = config['auto_leave_count']

    # Get the offset in seconds to join the meeting early
    join_early_offset = 60
    if "join_early_offset" in config and config['join_early_offset'] > 0:
        join_early_offset = config['join_early_offset']

    while 1:
        timestamp = datetime.now()
        # Check for new meetings if we are not currently in one
        if current_meeting is None:
            # Check if user has manually joined a meeting
            meeting_buttons = wait_until_found('.calling-unified-bar', 0, False)
            if meeting_buttons is not None:
                print("\nActive meeting detected, user has manually joined.")
                current_meeting = Meeting(None, None, None)
                continue

            print(f"\n[{timestamp:%H:%M:%S}] Looking for new meetings")

            # Look for meetings, then join one
            meetings = []
            switch_to_calendar_tab()
            get_calendar_meetings()
            if len(meetings) > 0:
                print("Found meetings: ", *meetings, sep='\n')
                meeting_to_join = decide_meeting()
                if meeting_to_join is not None:
                    join_meeting(meeting_to_join)
            # Check for new meetings after delay
            time.sleep(check_interval)

        elif current_meeting is not None:
            # Check if the user has manually left the meeting
            meeting_buttons = wait_until_found('.calling-unified-bar', 10)
            if meeting_buttons is None:
                print("\nNo active meeting detected, searching for new meeting.")
                current_meeting = None
                if hangup_thread:
                    hangup_thread.cancel()
                continue

            if (current_meeting is not None and auto_leave and
                    not current_meeting.auto_leave_blacklisted):

                # Check meeting member count to see if we need to leave
                members = get_meeting_members()
                print(f"\n[{timestamp:%H:%M:%S}]", "Current members:", members)
                if members and 0 < members <= auto_leave_count:
                    print("Last attendee in meeting")
                    hangup()
                    time.sleep(check_interval)
            else:
                print(f"\n[{timestamp:%H:%M:%S}] Monitoring meeting status...")

            # Check for members after delay
            time.sleep(member_interval)


if __name__ == "__main__":
    # Prevent computer from sleeping while script is running
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)

    # Calculate startup delay in seconds based on config
    load_config()
    if "run_at_time" in config and config['run_at_time'] != "":
        now = datetime.now()
        run_at = datetime.strptime(config['run_at_time'], "%H:%M").replace(
            year=now.year, month=now.month, day=now.day)

        if run_at.time() < now.time():
            run_at = datetime.strptime(config['run_at_time'], "%H:%M").replace(
                year=now.year, month=now.month, day=now.day + 1)

        start_delay = (run_at - now).total_seconds()
        time.sleep(start_delay)
        print(f"Waiting until {run_at} ({int(start_delay)}s)")
    try:
        main()
    except exceptions.WebDriverException:
        print("Selenium client unreachable, exiting...")
    finally:
        if browser is not None:
            browser.quit()
        if hangup_thread is not None:
            hangup_thread.cancel()
        input("Push enter to exit.")
