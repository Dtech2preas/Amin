import asyncio
import json
import os
import time
import hashlib
from playwright.async_api import async_playwright
import logging
from typing import Dict, List, Any
import aiofiles
import random

# Custom formatter that handles missing instance_id
class InstanceFormatter(logging.Formatter):
    def format(self, record):
        if not hasattr(record, 'instance_id'):
            record.instance_id = 'MAIN'
        return super().format(record)

# --- Set up logging ---
# These handlers are added to the root logger
file_handler = logging.FileHandler('iframe_extraction.log', encoding='utf-8')
file_handler.setFormatter(InstanceFormatter('%(asctime)s - %(levelname)s - [%(instance_id)s] %(message)s'))

console_handler = logging.StreamHandler()
console_handler.setFormatter(InstanceFormatter('%(asctime)s - %(levelname)s - [%(instance_id)s] %(message)s'))

# Get root logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(file_handler)
logger.addHandler(console_handler) # This will be removed in main()


class InstanceCoordinator:
    """Coordinates between multiple instances to avoid conflicts"""
    
    def __init__(self, coordination_dir=".coordination"):
        self.coordination_dir = coordination_dir
        os.makedirs(coordination_dir, exist_ok=True)
        self.instance_id = hashlib.md5(str(time.time()).encode()).hexdigest()[:8]
        
    def is_file_being_processed(self, file_path: str) -> bool:
        """Check if a file is currently being processed by another instance"""
        lock_file = os.path.join(self.coordination_dir, f"{os.path.basename(file_path)}.lock")
        if os.path.exists(lock_file):
            # Check if lock is stale (older than 10 minutes)
            if time.time() - os.path.getmtime(lock_file) > 600:
                try:
                    os.remove(lock_file)
                except OSError:
                    pass # Ignore if another instance removes it
                return False
            return True
        return False
    
    def lock_file(self, file_path: str):
        """Create a lock file to indicate this file is being processed"""
        lock_file = os.path.join(self.coordination_dir, f"{os.path.basename(file_path)}.lock")
        try:
            with open(lock_file, 'w') as f:
                f.write(self.instance_id)
        except IOError as e:
            logger.warning(f"Failed to create lock file {lock_file}: {e}", extra={'instance_id': self.instance_id or 'MAIN'})
    
    def unlock_file(self, file_path: str):
        """Remove lock file"""
        lock_file = os.path.join(self.coordination_dir, f"{os.path.basename(file_path)}.lock")
        if os.path.exists(lock_file):
            try:
                os.remove(lock_file)
            except OSError as e:
                logger.warning(f"Failed to remove lock file {lock_file}: {e}", extra={'instance_id': self.instance_id or 'MAIN'})
    
    def get_available_files(self, directory: str) -> List[str]:
        """Get list of JSON files that aren't being processed"""
        try:
            json_files = [f for f in os.listdir(directory) 
                         if f.startswith('anime_') and f.endswith('.json')]
        except FileNotFoundError:
            logger.error(f"Directory not found: {directory}", extra={'instance_id': self.instance_id or 'MAIN'})
            return []
        
        available_files = []
        for json_file in json_files:
            file_path = os.path.join(directory, json_file)
            if not self.is_file_being_processed(file_path):
                available_files.append(file_path)
        
        return available_files

class MultiInstanceAnimeExtractor:
    def __init__(self, instance_id: str, max_browsers: int = 3, max_tabs_per_browser: int = 3):
        self.instance_id = instance_id
        self.max_browsers = max_browsers
        self.max_tabs_per_browser = max_tabs_per_browser
        self.coordinator = InstanceCoordinator()
        self.processed_count = 0  # Episodes processed
        self.error_count = 0      # Episodes failed
        self.files_processed_count = 0 # Files completed
        self.browsers = []
        self.playwrights = []
        self.active_tasks = set()
        
        # Get the logger. Logs will propagate to the root logger.
        self.logger = logging.getLogger(f"Instance-{instance_id}")
        self.logger.setLevel(logging.INFO) # Ensure it captures INFO level
        
        # Add instance_id to all log messages from this class
        self.log_extra = {'instance_id': self.instance_id}
    
    async def init_browsers(self):
        """Initialize multiple browser instances"""
        self.logger.info(f"🚀 Initializing {self.max_browsers} browsers...", extra=self.log_extra)
        
        for i in range(self.max_browsers):
            try:
                playwright = await async_playwright().start()
                self.playwrights.append(playwright)
                
                chromium = await playwright.chromium.launch(
                    headless=True,
                    args=[
                        '--disable-blink-features=AutomationControlled',
                        '--no-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-gpu'
                    ]
                )
                self.browsers.append({
                    'browser': chromium,
                    'tabs_available': self.max_tabs_per_browser,
                    'tabs': []
                })
                self.logger.info(f"✅ Browser {i+1} initialized", extra=self.log_extra)
            except Exception as e:
                self.logger.error(f"❌ Failed to initialize browser {i+1}: {e}", extra=self.log_extra)
        
        for browser_idx, browser_info in enumerate(self.browsers):
            for tab_idx in range(self.max_tabs_per_browser):
                try:
                    context = await browser_info['browser'].new_context(
                        viewport={'width': 1920, 'height': 1080},
                        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                    )
                    
                    await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
                    
                    page = await context.new_page()
                    browser_info['tabs'].append(page)
                    self.logger.info(f"✅ Browser {browser_idx+1}, Tab {tab_idx+1} ready", extra=self.log_extra)
                    
                except Exception as e:
                    self.logger.error(f"❌ Failed to create tab {tab_idx+1} for browser {browser_idx+1}: {e}", extra=self.log_extra)
    
    async def get_available_tab(self):
        """Get an available tab from any browser"""
        while True:
            for browser_info in self.browsers:
                if browser_info['tabs_available'] > 0 and browser_info['tabs']:
                    browser_info['tabs_available'] -= 1
                    tab = browser_info['tabs'].pop(0)
                    return tab, browser_info
            # If no tab is available, wait a bit and retry
            await asyncio.sleep(0.5)
    
    async def release_tab(self, tab, browser_info):
        """Release a tab back to the pool"""
        browser_info['tabs'].append(tab)
        browser_info['tabs_available'] += 1
    
    async def extract_iframe_url(self, url: str, tab) -> str:
        """Extract iframe URL using an existing tab"""
        try:
            self.logger.info(f"🔍 Extracting iframe from: {url}", extra=self.log_extra)
            
            await tab.goto(url, wait_until='networkidle', timeout=60000)
            
            if 'DDoS-Guard' in await tab.title():
                self.logger.info("🛡️ Bypassing DDoS-Guard...", extra=self.log_extra)
                await tab.wait_for_function("() => !document.title.includes('DDoS-Guard')", timeout=60000)
            
            self.logger.info(f"✅ Loaded: {await tab.title()}", extra=self.log_extra)
            
            await tab.wait_for_timeout(2000)
            
            iframe_url = await self._find_iframe_directly(tab)
            if iframe_url: return iframe_url
            
            iframe_url = await self._find_iframe_in_javascript(tab)
            if iframe_url: return iframe_url
            
            iframe_url = await self._find_dynamic_iframe(tab)
            if iframe_url: return iframe_url
            
            iframe_url = await self._find_iframe_after_interaction(tab)
            if iframe_url: return iframe_url
            
            self.logger.warning(f"⚠️ No iframe found for: {url}", extra=self.log_extra)
            return None
            
        except Exception as e:
            self.logger.error(f"❌ Error extracting from {url}: {e}", extra=self.log_extra)
            return None
    
    async def _find_iframe_directly(self, tab):
        iframes = await tab.query_selector_all('iframe')
        for iframe in iframes:
            src = await iframe.get_attribute('src')
            if src:
                full_url = self._make_absolute_url(tab.url, src)
                if any(keyword in full_url.lower() for keyword in ['player', 'video', 'embed', 'kwik', 'stream']):
                    self.logger.info(f"✅ Found video player iframe: {full_url}", extra=self.log_extra)
                    return full_url
                elif 'animepahe' not in full_url:
                    self.logger.info(f"✅ Found external player iframe: {full_url}", extra=self.log_extra)
                    return full_url
        return None
    
    async def _find_iframe_in_javascript(self, tab):
        try:
            js_code = """
            () => {
                const results = { dataAttrs: [], scriptUrls: [] };
                document.querySelectorAll('[data-src], [data-embed], [data-iframe], [data-url]').forEach(el => {
                    Array.from(el.attributes).forEach(attr => {
                        if (attr.name.startsWith('data-') && attr.value.includes('http')) {
                            results.dataAttrs.push(attr.value);
                        }
                    });
                });
                document.querySelectorAll('script').forEach(script => {
                    const content = script.textContent || script.innerText;
                    const urlMatches = content.match(/(https?:\\/\\/[^"']+)/g);
                    if (urlMatches) {
                        urlMatches.forEach(url => {
                            if (url.includes('embed') || url.includes('player') || url.includes('kwik')) {
                                results.scriptUrls.push(url);
                            }
                        });
                    }
                });
                return results;
            }
            """
            result = await tab.evaluate(js_code)
            if result:
                for url in result['dataAttrs'] + result['scriptUrls']:
                    full_url = self._make_absolute_url(tab.url, url)
                    if any(keyword in full_url.lower() for keyword in ['embed', 'player', 'kwik']):
                        self.logger.info(f"✅ Found iframe in JS/data: {full_url}", extra=self.log_extra)
                        return full_url
        except Exception as e:
            self.logger.error(f"❌ JavaScript search failed: {e}", extra=self.log_extra)
        return None
    
    async def _find_dynamic_iframe(self, tab):
        player_selectors = [
            '#player', '.player', '#video-player', '.video-player',
            '#embed-player', '.embed-player', '[id*="player"]',
            '[class*="player"]', '.pahe-player', '#kwikPlayer'
        ]
        for selector in player_selectors:
            elements = await tab.query_selector_all(selector)
            for element in elements:
                iframe = await element.query_selector('iframe')
                if iframe:
                    src = await iframe.get_attribute('src')
                    if src:
                        full_url = self._make_absolute_url(tab.url, src)
                        self.logger.info(f"✅ Found iframe in player container: {full_url}", extra=self.log_extra)
                        return full_url
                attrs = ['data-src', 'data-embed', 'data-iframe', 'data-url']
                for attr in attrs:
                    value = await element.get_attribute(attr)
                    if value and 'http' in value:
                        full_url = self._make_absolute_url(tab.url, value)
                        self.logger.info(f"✅ Found iframe URL in data attribute: {full_url}", extra=self.log_extra)
                        return full_url
        return None
    
    async def _find_iframe_after_interaction(self, tab):
        play_buttons = ['.play-button', '[class*="play"]', '.btn-play', 'button[onclick*="embed"]', 'a[href*="embed"]']
        iframe_requests = []
        
        async def capture_iframe_requests(request):
            url = request.url
            if any(keyword in url for keyword in ['embed', 'player', 'kwik']):
                iframe_requests.append(url)
        
        tab.on("request", capture_iframe_requests)
        
        for button_selector in play_buttons:
            try:
                buttons = await tab.query_selector_all(button_selector)
                for button in buttons:
                    try:
                        await button.click()
                        await tab.wait_for_timeout(2000)
                        iframe_url = await self._find_iframe_directly(tab)
                        if iframe_url:
                            tab.off("request", capture_iframe_requests)
                            return iframe_url
                        if iframe_requests:
                            tab.off("request", capture_iframe_requests)
                            return iframe_requests[-1]
                    except Exception: continue
            except Exception: continue
        
        tab.off("request", capture_iframe_requests)
        return None
    
    def _make_absolute_url(self, base_url, relative_url):
        if not relative_url: return relative_url
        if relative_url.startswith(('http://', 'https://')): return relative_url
        if relative_url.startswith('//'): return 'https:' + relative_url
        from urllib.parse import urlparse
        parsed_base = urlparse(base_url)
        if relative_url.startswith('/'):
            return f"{parsed_base.scheme}://{parsed_base.netloc}{relative_url}"
        else:
            return f"{parsed_base.scheme}://{parsed_base.netloc}/{relative_url}"

    async def process_episode_batch(self, episodes_batch: List[Dict]) -> List[Dict]:
        tasks = []
        for episode in episodes_batch:
            task = asyncio.create_task(self.process_single_episode(episode))
            tasks.append(task)
            self.active_tasks.add(task)
            task.add_done_callback(self.active_tasks.discard)
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        updated_episodes = []
        for episode, result in zip(episodes_batch, results):
            if isinstance(result, Exception):
                self.logger.error(f"❌ Error processing episode: {result}", extra=self.log_extra)
                episode['iframe_url'] = None # Modify in-place
                self.error_count += 1
            else:
                episode['iframe_url'] = result # Modify in-place
                if result:
                    self.processed_count += 1
                else:
                    self.error_count += 1
            updated_episodes.append(episode)
        return updated_episodes
    
    async def process_single_episode(self, episode: Dict) -> str:
        tab, browser_info = await self.get_available_tab()
        try:
            episode_url = episode.get('url')
            if not episode_url: return None
            iframe_url = await self.extract_iframe_url(episode_url, tab)
            return iframe_url
        finally:
            await self.release_tab(tab, browser_info)
            await asyncio.sleep(random.uniform(1, 3))

    # --- THIS IS THE MODIFIED FUNCTION ---
    async def process_file(self, file_path: str):
        """
        Process a single JSON file.
        NEW LOGIC:
        1. Lock the file.
        2. Read all pending episodes into one list.
        3. Process in batches matching instance capacity (e.g., 9 tabs).
        4. Save the file to disk *after every batch* to prevent data loss.
        5. Unlock when done.
        """
        self.logger.info(f"📁 Processing file: {file_path}", extra=self.log_extra)
        
        try:
            # Lock the file
            self.coordinator.lock_file(file_path)
            
            # Read the file
            async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                content = await f.read()
                data = json.loads(content)
            
            # --- NEW LOGIC: Gather all episodes first ---
            all_pending_episodes = []
            for anime in data.get('anime', []):
                for episode in anime.get('episodes', []):
                    # Process only if iframe_url is missing or None
                    if not episode.get('iframe_url'):
                        # Add a reference to the episode dict
                        all_pending_episodes.append(episode) 
            
            if not all_pending_episodes:
                self.logger.info(f"✅ File {file_path} is already fully processed.", extra=self.log_extra)
                self.files_processed_count += 1
                return True

            self.logger.info(f"🎬 Found {len(all_pending_episodes)} pending episodes in {file_path}.", extra=self.log_extra)
            
            # --- NEW LOGIC: Use full capacity for batch size ---
            batch_size = self.max_browsers * self.max_tabs_per_browser 
            
            total_batches = (len(all_pending_episodes) + batch_size - 1) // batch_size
            
            for i in range(0, len(all_pending_episodes), batch_size):
                batch = all_pending_episodes[i:i + batch_size]
                self.logger.info(f"🔧 Processing batch {i//batch_size + 1} of {total_batches} (Size: {len(batch)})", extra=self.log_extra)
                
                # Process the batch (this modifies the episodes in-place)
                # The 'data' object is updated because 'batch' contains references
                await self.process_episode_batch(batch)
                
                # --- NEW LOGIC: Save after every batch ---
                self.logger.info(f"💾 Saving progress for {file_path}...", extra=self.log_extra)
                async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
                    await f.write(json.dumps(data, indent=2, ensure_ascii=False))
            
            # All batches done
            self.logger.info(f"✅ Finished and saved file: {file_path}", extra=self.log_extra)
            self.files_processed_count += 1
            return True
            
        except Exception as e:
            self.logger.error(f"💥 Error processing file {file_path}: {e}", extra=self.log_extra)
            return False
        finally:
            # Unlock the file
            self.coordinator.unlock_file(file_path)

    async def run_instance(self, directory: str):
        self.logger.info(f"🚀 Instance {self.instance_id} starting...", extra=self.log_extra)
        await self.init_browsers()
        
        try:
            while True:
                available_files = self.coordinator.get_available_files(directory)
                
                if not available_files:
                    self.logger.info("📭 No more files to process. Waiting...", extra=self.log_extra)
                    await asyncio.sleep(30)
                    available_files = self.coordinator.get_available_files(directory)
                    if not available_files:
                        self.logger.info("🏁 No files available. Shutting down.", extra=self.log_extra)
                        break
                
                file_to_process = random.choice(available_files)
                self.logger.info(f"🎯 Selected file: {os.path.basename(file_to_process)}", extra=self.log_extra)
                
                await self.process_file(file_to_process)
                
                await asyncio.sleep(5)
                
        except Exception as e:
            self.logger.error(f"💥 Instance error: {e}", extra=self.log_extra)
        finally:
            await self.cleanup()
    
    async def cleanup(self):
        self.logger.info(f"🧹 Cleaning up instance {self.instance_id}...", extra=self.log_extra)
        for browser_info in self.browsers:
            try: await browser_info['browser'].close()
            except Exception as e: self.logger.error(f"❌ Error closing browser: {e}", extra=self.log_extra)
        for playwright in self.playwrights:
            try: await playwright.stop()
            except Exception as e: self.logger.error(f"❌ Error stopping playwright: {e}", extra=self.log_extra)

async def get_total_pending_episodes(directory: str) -> (int, int):
    """Scans all files to get a total count of pending episodes."""
    logger.info("🔍 Starting pre-scan to count total episodes...", extra={'instance_id': 'MAIN'})
    coordinator = InstanceCoordinator()
    all_files = coordinator.get_available_files(directory)
    
    total_pending_episodes = 0
    initial_file_count = len(all_files)
    
    for file_path in all_files:
        try:
            async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                content = await f.read()
                data = json.loads(content)
                
                for anime in data.get('anime', []):
                    for episode in anime.get('episodes', []):
                        if not episode.get('iframe_url'):
                            total_pending_episodes += 1
        except Exception as e:
            logger.error(f"Failed to scan file {file_path}: {e}", extra={'instance_id': 'MAIN'})
            
    logger.info(f"✅ Pre-scan complete. Found {total_pending_episodes} pending episodes in {initial_file_count} files.", extra={'instance_id': 'MAIN'})
    return total_pending_episodes, initial_file_count

async def display_dashboard(instances: List[MultiInstanceAnimeExtractor], 
                          coordinator: InstanceCoordinator, 
                          directory: str, 
                          total_episodes: int, 
                          initial_file_count: int, 
                          instance_tasks: List[asyncio.Task]):
    """Clears the screen and displays a fixed dashboard."""
    
    start_time = time.time()
    
    def clear_screen():
        os.system('cls' if os.name == 'nt' else 'clear')

    def format_time(seconds):
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    while True:
        clear_screen()
        
        # --- Calculate Global Stats ---
        active_instances = sum(1 for task in instance_tasks if not task.done())
        
        # We sum files processed by instances, as coordinator.get_available_files() can fluctuate
        files_processed_total = sum(inst.files_processed_count for inst in instances)
        files_pct = (files_processed_total / initial_file_count) * 100 if initial_file_count > 0 else 0
        
        total_episodes_processed = sum(inst.processed_count for inst in instances)
        total_errors = sum(inst.error_count for inst in instances)
        
        # Total episodes to process might be 0
        episodes_pct = (total_episodes_processed / total_episodes) * 100 if total_episodes > 0 else 0
        episodes_left = total_episodes - total_episodes_processed
        
        total_active_tasks = sum(len(inst.active_tasks) for inst in instances)
        total_capacity = sum(inst.max_browsers * inst.max_tabs_per_browser for inst in instances)
        
        elapsed_time = time.time() - start_time
        
        # --- Print Dashboard ---
        print("=" * 60)
        print(f"📊     ANIME EXTRACTOR DASHBOARD (Updates every 2 mins)     📊")
        print(f"         (Logs are being written to 'iframe_extraction.log')")
        print("=" * 60)
        
        print(f"\n--- 🌎 GLOBAL STATUS (Elapsed: {format_time(elapsed_time)}) ---")
        print(f"Instances Active:   {active_instances} / {len(instances)}")
        print(f"Active Tasks (Tabs):{total_active_tasks: >3} / {total_capacity:<3}")
        print(f"---")
        print(f"Files Processed:    {files_processed_total: >5} / {initial_file_count:<5}  [{files_pct:6.2f}%]")
        print(f"Episodes Processed: {total_episodes_processed: >5} / {total_episodes:<5}  [{episodes_pct:6.2f}%]")
        print(f"Episodes Left:      {episodes_left: >5}")
        print(f"Total Errors:       {total_errors: >5}")
        
        print(f"\n--- 📦 INSTANCE DETAILS ---")
        
        for inst in instances:
            inst_active_tasks = len(inst.active_tasks)
            inst_capacity = inst.max_browsers * inst.max_tabs_per_browser
            task_status = "Running" if not instance_tasks[instances.index(inst)].done() else "Stopped"
            
            print(f"\n[{inst.instance_id}] ({task_status})")
            print(f"  Tasks: {inst_active_tasks: >3} / {inst_capacity:<3}   | Files Done: {inst.files_processed_count: >4}")
            print(f"  Eps Done: {inst.processed_count: >5} | Eps Error:  {inst.error_count: >4}")

        print("\n" + "=" * 60)
        print("Press Ctrl+C to stop...")
        
        # Check if all instances are done
        if active_instances == 0:
            logger.info("Dashboard: All instances finished.", extra={'instance_id': 'MAIN'})
            break
            
        await asyncio.sleep(120) # Update every 2 minutes

async def main():
    print("🎬 AnimePahe Multi-Instance Batch Iframe Extractor")
    print("=" * 50)
    
    directory = input("Enter directory path (press Enter for current directory): ").strip()
    if not directory:
        directory = "."
    
    if not os.path.exists(directory):
        print(f"❌ Directory '{directory}' does not exist!")
        return
    
    num_instances = int(input("Enter number of instances to run (1-8): ").strip() or "3")
    num_instances = max(1, min(8, num_instances))
    
    browsers_per_instance = int(input("Enter browsers per instance (1-3): ").strip() or "2")
    tabs_per_browser = int(input("Enter tabs per browser (1-3): ").strip() or "2")
    
    print(f"\n🚀 Starting {num_instances} instances...")
    print(f"📁 Directory: {os.path.abspath(directory)}")
    print(f"🔧 Total concurrent operations: {num_instances * browsers_per_instance * tabs_per_browser}")
    
    confirm = input("\n⚠️  This will modify your JSON files. Continue? (y/N): ").strip().lower()
    if confirm not in ['y', 'yes']:
        print("❌ Operation cancelled.")
        return
    
    # --- Run Pre-Scan ---
    total_episodes, initial_file_count = await get_total_pending_episodes(directory)
    if initial_file_count == 0:
        print("No 'anime_*.json' files found to process. Exiting.")
        return
    if total_episodes == 0:
        print("All episodes in all files have already been processed. Exiting.")
        return
        
    print(f"Found {initial_file_count} files and {total_episodes} pending episodes.")
    
    # --- Disable Console Logging for Dashboard ---
    logger.info("Disabling console logging and starting dashboard...", extra={'instance_id': 'MAIN'})
    root_logger = logging.getLogger()
    
    # We defined console_handler at the top of the script. Remove it.
    root_logger.removeHandler(console_handler)
    console_handler.close()
        
    print("\nConsole logging disabled. View 'iframe_extraction.log' for details.")
    print("Starting dashboard in 3 seconds...")
    await asyncio.sleep(3)
    
    # --- Create and run instances ---
    instances = []
    instance_tasks = []
    coordinator = InstanceCoordinator() # Single coordinator
    
    for i in range(num_instances):
        instance_id = f"INST-{i+1:02d}"
        extractor = MultiInstanceAnimeExtractor(
            instance_id=instance_id,
            max_browsers=browsers_per_instance,
            max_tabs_per_browser=tabs_per_browser
        )
        instances.append(extractor)
        task = asyncio.create_task(extractor.run_instance(directory))
        instance_tasks.append(task)
        
        await asyncio.sleep(2) # Stagger startup
    
    # --- Start Dashboard Task ---
    dashboard_task = asyncio.create_task(display_dashboard(
        instances, 
        coordinator, 
        directory, 
        total_episodes, 
        initial_file_count,
        instance_tasks
    ))
    
    # Wait for all instances and the dashboard to complete
    all_tasks = instance_tasks + [dashboard_task]
    try:
        await asyncio.gather(*all_tasks)
    except KeyboardInterrupt:
        print("\n🛑 Shutting down instances...")
        # Cancel all tasks on Ctrl+C
        for task in all_tasks:
            task.cancel()
        # Wait for them to actually cancel
        await asyncio.gather(*all_tasks, return_exceptions=True)
    finally:
        # Final cleanup
        for instance in instances:
            await instance.cleanup()
        
        print("\n🎉 All instances stopped!")
        
        # --- Generate final summary ---
        total_processed = sum(instance.processed_count for instance in instances)
        total_errors = sum(instance.error_count for instance in instances)
        
        print("=" * 50)
        print("📊 FINAL SUMMARY")
        print("=" * 50)
        print(f"✅ Total iframe URLs extracted: {total_processed}")
        print(f"❌ Total errors: {total_errors}")
        
        for instance in instances:
            print(f"📦 {instance.instance_id}: {instance.processed_count} processed, {instance.error_count} errors")

if __name__ == "__main__":
    asyncio.run(main())
