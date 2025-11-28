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

# Set up logging
file_handler = logging.FileHandler('iframe_extraction.log', encoding='utf-8')
file_handler.setFormatter(InstanceFormatter('%(asctime)s - %(levelname)s - [%(instance_id)s] %(message)s'))

console_handler = logging.StreamHandler()
console_handler.setFormatter(InstanceFormatter('%(asctime)s - %(levelname)s - [%(instance_id)s] %(message)s'))

logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, console_handler]
)
logger = logging.getLogger(__name__)

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
            if time.time() - os.path.getmtime(lock_file) > 600:
                os.remove(lock_file)
                return False
            return True
        return False
    
    def lock_file(self, file_path: str):
        """Create a lock file to indicate this file is being processed"""
        lock_file = os.path.join(self.coordination_dir, f"{os.path.basename(file_path)}.lock")
        with open(lock_file, 'w') as f:
            f.write(self.instance_id)
    
    def unlock_file(self, file_path: str):
        """Remove lock file"""
        lock_file = os.path.join(self.coordination_dir, f"{os.path.basename(file_path)}.lock")
        if os.path.exists(lock_file):
            os.remove(lock_file)
    
    def get_available_files(self, directory: str) -> List[str]:
        """Get list of JSON files that aren't being processed"""
        json_files = [f for f in os.listdir(directory) 
                     if f.startswith('anime_') and f.endswith('.json')]
        
        available_files = []
        for json_file in json_files:
            file_path = os.path.join(directory, json_file)
            if not self.is_file_being_processed(file_path):
                available_files.append(file_path)
        
        return available_files

class GlobalProgress:
    """Shared progress tracking across all instances"""
    def __init__(self):
        self.completed_files = set()
        self.currently_processing = {}  # {file_path: instance_id}
        self.total_files = 0
        self.lock = asyncio.Lock()
        
        # --- NEW --- Episode-level tracking
        self.total_episodes = 0
        self.completed_episodes = 0
    
    async def mark_file_started(self, file_path: str, instance_id: str):
        async with self.lock:
            self.currently_processing[file_path] = instance_id
    
    async def mark_file_completed(self, file_path: str):
        async with self.lock:
            self.completed_files.add(file_path)
            if file_path in self.currently_processing:
                del self.currently_processing[file_path]
    
    # --- NEW --- Method to increment episode count
    async def increment_completed_episodes(self, count=1):
        async with self.lock:
            self.completed_episodes += count
    
    async def get_status(self):
        async with self.lock:
            return {
                'completed': len(self.completed_files),
                'processing': dict(self.currently_processing),
                'total': self.total_files,
                'remaining': self.total_files - len(self.completed_files) - len(self.currently_processing)
            }

class FailureLogger:
    """Logs failed episodes to a file for reprocessing"""
    def __init__(self, filename="failed_episodes.jsonl"):
        self.filename = filename
        self.lock = asyncio.Lock()

    async def log_failure(self, episode_data, reason):
        log_entry = {
            "timestamp": time.time(),
            "reason": reason,
            "episode": episode_data
        }
        async with self.lock:
            async with aiofiles.open(self.filename, 'a', encoding='utf-8') as f:
                await f.write(json.dumps(log_entry) + "\n")

class MultiInstanceAnimeExtractor:
    def __init__(self, instance_id: str, max_browsers: int = 3, max_tabs_per_browser: int = 3, global_progress=None, failure_logger=None):
        self.instance_id = instance_id
        self.max_browsers = max_browsers
        self.max_tabs_per_browser = max_tabs_per_browser
        self.coordinator = InstanceCoordinator()
        self.processed_count = 0  # Total processed by this instance
        self.error_count = 0      # Total errors by this instance
        self.browsers = []
        self.playwrights = []
        self.active_tasks = set()
        self.global_progress = global_progress
        self.failure_logger = failure_logger
        self.current_file = None
        
        # --- NEW --- Per-file progress tracking
        self.current_file_progress = {
            "file_name": "Idle",
            "total_episodes": 0,
            "processed": 0, # Processed *this run*
            "skipped": 0,   # Already complete
            "remaining": 0
        }
        
        # Add instance ID to logger
        for handler in logger.handlers:
            handler.setFormatter(logging.Formatter(
                '%(asctime)s - %(levelname)s - [%(instance_id)s] %(message)s'
            ))
    
    async def init_browsers(self):
        """Initialize multiple browser instances"""
        logger.info(f"🚀 Initializing {self.max_browsers} browsers...", extra={'instance_id': self.instance_id})
        
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
                logger.info(f"✅ Browser {i+1} initialized", extra={'instance_id': self.instance_id})
            except Exception as e:
                logger.error(f"❌ Failed to initialize browser {i+1}: {e}", extra={'instance_id': self.instance_id})
        
        # Initialize tabs for each browser
        for browser_idx, browser_info in enumerate(self.browsers):
            for tab_idx in range(self.max_tabs_per_browser):
                try:
                    context = await browser_info['browser'].new_context(
                        viewport={'width': 1920, 'height': 1080},
                        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                    )
                    
                    # --- KEPT FOR SPEED ---
                    await context.route("**/*", lambda route:
                        route.abort() if route.request.resource_type in {
                            "image", "stylesheet", "font", "media", "other" 
                        } else route.continue_()
                    )
                    
                    await context.add_init_script("""
                        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    """)
                    
                    page = await context.new_page()
                    browser_info['tabs'].append(page)
                    logger.info(f"✅ Browser {browser_idx+1}, Tab {tab_idx+1} ready", extra={'instance_id': self.instance_id})
                    
                except Exception as e:
                    logger.error(f"❌ Failed to create tab {tab_idx+1} for browser {browser_idx+1}: {e}", extra={'instance_id': self.instance_id})
    
    async def get_available_tab(self):
        """Get an available tab from any browser"""
        for browser_info in self.browsers:
            if browser_info['tabs_available'] > 0 and browser_info['tabs']:
                browser_info['tabs_available'] -= 1
                tab = browser_info['tabs'].pop(0)
                return tab, browser_info
        return None, None
    
    async def release_tab(self, tab, browser_info):
        """Release a tab back to the pool"""
        browser_info['tabs'].append(tab)
        browser_info['tabs_available'] += 1
    
    # --- This is your original, tuned extraction function ---
    async def extract_iframe_url(self, url: str, tab) -> str:
        """Extract iframe URL using an existing tab"""
        try:
            logger.info(f"🔍 Extracting iframe from: {url}", extra={'instance_id': self.instance_id})
            
            await tab.goto(url, wait_until='networkidle', timeout=60000)
            
            if 'DDoS-Guard' in await tab.title():
                logger.info("🛡️ Bypassing DDoS-Guard...", extra={'instance_id': self.instance_id})
                await tab.wait_for_function("""
                    () => !document.title.includes('DDoS-Guard')
                """, timeout=60000)
            
            logger.info(f"✅ Loaded: {await tab.title()}", extra={'instance_id': self.instance_id})
            
            await tab.wait_for_timeout(2000)
            
            iframe_url = await self._find_iframe_directly(tab)
            if iframe_url: return iframe_url
            
            iframe_url = await self._find_iframe_in_javascript(tab)
            if iframe_url: return iframe_url
            
            iframe_url = await self._find_dynamic_iframe(tab)
            if iframe_url: return iframe_url
            
            iframe_url = await self._find_iframe_after_interaction(tab)
            if iframe_url: return iframe_url
            
            return None
            
        except Exception as e:
            logger.error(f"❌ Error extracting from {url}: {e}", extra={'instance_id': self.instance_id})
            return None
    
    async def _find_iframe_directly(self, tab):
        iframes = await tab.query_selector_all('iframe')
        for iframe in iframes:
            src = await iframe.get_attribute('src')
            if src:
                full_url = self._make_absolute_url(tab.url, src)
                if any(keyword in full_url.lower() for keyword in ['player', 'video', 'embed', 'kwik', 'stream']):
                    logger.info(f"✅ Found video player iframe: {full_url}", extra={'instance_id': self.instance_id})
                    return full_url
                elif 'animepahe' not in full_url:
                    logger.info(f"✅ Found external player iframe: {full_url}", extra={'instance_id': self.instance_id})
                    return full_url
        return None
    
    async def _find_iframe_in_javascript(self, tab):
        try:
            js_code = """
            () => {
                const results = { dataAttrs: [], scriptUrls: [] };
                const elements = document.querySelectorAll('[data-src], [data-embed], [data-iframe], [data-url]');
                elements.forEach(el => {
                    Array.from(el.attributes).forEach(attr => {
                        if (attr.name.startsWith('data-') && attr.value.includes('http')) {
                            results.dataAttrs.push(attr.value);
                        }
                    });
                });
                const scripts = document.querySelectorAll('script');
                scripts.forEach(script => {
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
                for url in result['dataAttrs']:
                    full_url = self._make_absolute_url(tab.url, url)
                    if any(keyword in full_url.lower() for keyword in ['embed', 'player', 'kwik']):
                        logger.info(f"✅ Found iframe in data attribute: {full_url}", extra={'instance_id': self.instance_id})
                        return full_url
                for url in result['scriptUrls']:
                    full_url = self._make_absolute_url(tab.url, url)
                    if any(keyword in full_url.lower() for keyword in ['embed', 'player', 'kwik']):
                        logger.info(f"✅ Found iframe in script: {full_url}", extra={'instance_id': self.instance_id})
                        return full_url
        except Exception as e:
            logger.error(f"❌ JavaScript search failed: {e}", extra={'instance_id': self.instance_id})
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
                        logger.info(f"✅ Found iframe in player container: {full_url}", extra={'instance_id': self.instance_id})
                        return full_url
                attrs = ['data-src', 'data-embed', 'data-iframe', 'data-url']
                for attr in attrs:
                    value = await element.get_attribute(attr)
                    if value and 'http' in value:
                        full_url = self._make_absolute_url(tab.url, value)
                        logger.info(f"✅ Found iframe URL in data attribute: {full_url}", extra={'instance_id': self.instance_id})
                        return full_url
        return None
    
    async def _find_iframe_after_interaction(self, tab):
        play_buttons = [
            '.play-button', '[class*="play"]', '.btn-play',
            'button[onclick*="embed"]', 'a[href*="embed"]'
        ]
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
                        if iframe_url: return iframe_url
                        if iframe_requests: return iframe_requests[-1]
                    except Exception: continue
            except Exception: continue
        return None
    
    def _make_absolute_url(self, base_url, relative_url):
        if not relative_url: return relative_url
        if relative_url.startswith(('http://', 'https://')): return relative_url
        elif relative_url.startswith('//'): return 'https:' + relative_url
        elif relative_url.startswith('/'):
            from urllib.parse import urlparse
            parsed_base = urlparse(base_url)
            return f"{parsed_base.scheme}://{parsed_base.netloc}{relative_url}"
        else: return base_url + '/' + relative_url

    async def process_episode_batch(self, episodes_batch: List[Dict]) -> (List[Dict], int, int):
        """Process a batch of episodes concurrently"""
        tasks = []
        
        for episode in episodes_batch:
            task = asyncio.create_task(self.process_single_episode(episode))
            tasks.append(task)
            self.active_tasks.add(task)
            task.add_done_callback(self.active_tasks.discard)
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        updated_episodes = []
        
        # --- NEW --- Count successes/errors in this batch
        processed_in_batch = 0
        errors_in_batch = 0
        
        for episode, result in zip(episodes_batch, results):
            if isinstance(result, Exception):
                logger.error(f"❌ Error processing episode: {result}", extra={'instance_id': self.instance_id})
                episode['iframe_url'] = None
                self.error_count += 1
                errors_in_batch += 1
                if self.failure_logger:
                    await self.failure_logger.log_failure(episode, str(result))
            else:
                episode['iframe_url'] = result
                if result:
                    self.processed_count += 1
                    processed_in_batch += 1
                    # --- NEW --- Report to global progress
                    if self.global_progress:
                        await self.global_progress.increment_completed_episodes()
                else:
                    self.error_count += 1
                    errors_in_batch += 1
                    if self.failure_logger:
                        await self.failure_logger.log_failure(episode, "No iframe found (None result)")
            updated_episodes.append(episode)
        
        # --- CHANGED --- Return batch counts
        return updated_episodes, processed_in_batch, errors_in_batch
    
    async def process_single_episode(self, episode: Dict) -> str:
        """Process a single episode to get iframe URL"""
        tab, browser_info = await self.get_available_tab()
        if not tab:
            await asyncio.sleep(1)
            return await self.process_single_episode(episode)  # Retry
        
        try:
            episode_url = episode.get('url')
            if not episode_url:
                return None
            
            iframe_url = await self.extract_iframe_url(episode_url, tab)
            return iframe_url
            
        finally:
            await self.release_tab(tab, browser_info)
            await asyncio.sleep(random.uniform(1, 3))

    async def process_file(self, file_path: str):
        """Process a single JSON file"""
        self.current_file = os.path.basename(file_path)
        if self.global_progress:
            await self.global_progress.mark_file_started(file_path, self.instance_id)
        
        logger.info(f"📁 Processing file: {file_path}", extra={'instance_id': self.instance_id})
        
        try:
            self.coordinator.lock_file(file_path)
            
            async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                content = await f.read()
                data = json.loads(content)
            
            # --- NEW --- Calculate per-file stats
            total_episodes = 0
            skipped_episodes = 0
            episodes_to_process = []
            
            for anime in data.get('anime', []):
                # logger.info(f"🎬 Processing anime: {anime.get('title', 'Unknown')}", extra={'instance_id': self.instance_id}) # --- REMOVED for cleaner log
                
                for episode in anime.get('episodes', []):
                    total_episodes += 1
                    if episode.get('iframe_url'):
                        skipped_episodes += 1
                    else:
                        episodes_to_process.append(episode)
            
            # --- NEW --- Set initial file progress
            self.current_file_progress = {
                "file_name": self.current_file,
                "total_episodes": total_episodes,
                "processed": 0, # Just processed *by this instance*
                "skipped": skipped_episodes,
                "remaining": len(episodes_to_process)
            }
            logger.info(f"📊 File stats for {self.current_file}: {total_episodes} total, {skipped_episodes} skipped, {len(episodes_to_process)} to process.", extra={'instance_id': self.instance_id})
            
            batch_size = 5
            
            # Process episodes in batches
            for i in range(0, len(episodes_to_process), batch_size):
                batch = episodes_to_process[i:i + batch_size]
                logger.info(f"🔧 Processing batch {i//batch_size + 1} of {len(episodes_to_process)//batch_size + 1} for {self.current_file}", extra={'instance_id': self.instance_id})
                
                # --- CHANGED --- Get batch counts
                updated_batch, processed_in_batch, errors_in_batch = await self.process_episode_batch(batch)
                
                # --- NEW --- Update per-file progress
                self.current_file_progress["processed"] += processed_in_batch
                self.current_file_progress["remaining"] -= (processed_in_batch + errors_in_batch)
                
                # Update the episodes in the data
                for updated_episode in updated_batch:
                    for anime in data.get('anime', []):
                        for j, original_episode in enumerate(anime.get('episodes', [])):
                            if original_episode.get('url') == updated_episode.get('url'):
                                anime['episodes'][j] = updated_episode
                                break
            
            async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(data, indent=2, ensure_ascii=False))
            
            logger.info(f"💾 Updated file: {file_path}", extra={'instance_id': self.instance_id})
            
            if self.global_progress:
                await self.global_progress.mark_file_completed(file_path)
            
            # --- NEW --- Reset progress
            self.current_file = None
            self.current_file_progress["file_name"] = "Idle"
            return True
            
        except Exception as e:
            logger.error(f"💥 Error processing file {file_path}: {e}", extra={'instance_id': self.instance_id})
            if self.global_progress:
                await self.global_progress.mark_file_completed(file_path) # Mark as 'complete' to avoid retry loop
            
            # --- NEW --- Reset progress
            self.current_file = None
            self.current_file_progress["file_name"] = "Idle"
            return False
        finally:
            self.coordinator.unlock_file(file_path)

    async def run_instance(self, directory: str):
        """Main loop for this instance"""
        logger.info(f"🚀 Instance {self.instance_id} starting...", extra={'instance_id': self.instance_id})
        
        await self.init_browsers()
        
        try:
            while True:
                available_files = self.coordinator.get_available_files(directory)
                
                if not available_files:
                    logger.info("📭 No more files to process. Waiting...", extra={'instance_id': self.instance_id})
                    await asyncio.sleep(30)
                    
                    available_files = self.coordinator.get_available_files(directory)
                    if not available_files:
                        logger.info("🏁 No files available. Shutting down.", extra={'instance_id': self.instance_id})
                        break
                
                file_to_process = random.choice(available_files)
                logger.info(f"🎯 Selected file: {os.path.basename(file_to_process)}", extra={'instance_id': self.instance_id})
                
                success = await self.process_file(file_to_process)
                
                if success:
                    logger.info(f"✅ Completed: {os.path.basename(file_to_process)}", extra={'instance_id': self.instance_id})
                else:
                    logger.error(f"❌ Failed: {os.path.basename(file_to_process)}", extra={'instance_id': self.instance_id})
                
                await asyncio.sleep(5)
                
        except Exception as e:
            logger.error(f"💥 Instance error: {e}", extra={'instance_id': self.instance_id})
        finally:
            await self.cleanup()
    
    async def cleanup(self):
        """Clean up browsers and playwright instances"""
        logger.info(f"🧹 Cleaning up instance {self.instance_id}...", extra={'instance_id': self.instance_id})
        for browser_info in self.browsers:
            try: await browser_info['browser'].close()
            except Exception as e: logger.error(f"❌ Error closing browser: {e}", extra={'instance_id': self.instance_id})
        for playwright in self.playwrights:
            try: await playwright.stop()
            except Exception as e: logger.error(f"❌ Error stopping playwright: {e}", extra={'instance_id': self.instance_id})

# --- CHANGED --- Rewritten progress monitor for new episode-based logging
async def progress_monitor(global_progress: GlobalProgress, instances: List[MultiInstanceAnimeExtractor]):
    """Monitor and display progress every 2 minutes"""
    logger.info("📊 Progress monitor started. First report in 2 minutes.", extra={'instance_id': 'MONITOR'})
    
    while True:
        await asyncio.sleep(120)  # 2 minutes
        
        # Get file-level status
        status = await global_progress.get_status() 
        
        # Get Global Episode Status
        global_ep_completed = 0
        global_ep_total = 0
        async with global_progress.lock:
            global_ep_completed = global_progress.completed_episodes
            global_ep_total = global_progress.total_episodes
        
        logger.info("=" * 80, extra={'instance_id': 'MONITOR'})
        logger.info("📊 PROGRESS REPORT", extra={'instance_id': 'MONITOR'})
        logger.info("=" * 80, extra={'instance_id': 'MONITOR'})
        
        # --- NEW: Global Episode Progress ---
        if global_ep_total > 0:
            global_percent = (global_ep_completed / global_ep_total) * 100
            logger.info(
                f"📈 GLOBAL EPISODE PROGRESS: {global_ep_completed} / {global_ep_total} ({global_percent:.2f}%)",
                extra={'instance_id': 'MONITOR'}
            )
        
        logger.info(f"🗂️  GLOBAL FILE PROGRESS: {status['completed']} / {status['total']} files completed", extra={'instance_id': 'MONITOR'})
        
        # --- NEW: Per-File Processing Details ---
        logger.info("", extra={'instance_id': 'MONITOR'})
        logger.info("🔄 FILES CURRENTLY BEING PROCESSED:", extra={'instance_id': 'MONITOR'})
        
        found_processing = False
        active_instance_files = []
        for instance in instances:
            # Access the progress dict directly
            progress = instance.current_file_progress 
            if progress["file_name"] != "Idle":
                found_processing = True
                active_instance_files.append(progress["file_name"])
                
                total_done = progress['processed'] + progress['skipped']
                percentage = (total_done / progress['total_episodes']) * 100 if progress['total_episodes'] > 0 else 0.0
                
                logger.info(
                    f"  • {progress['file_name']} (on {instance.instance_id}): "
                    f"{total_done}/{progress['total_episodes']} ({percentage:.2f}%) "
                    f"| (New: {progress['processed']}, Skipped: {progress['skipped']})",
                    extra={'instance_id': 'MONITOR'}
                )
        
        if not found_processing:
            logger.info("  (All instances are currently idle or switching files)", extra={'instance_id': 'MONITOR'})
        # --- END NEW ---
        
        logger.info("", extra={'instance_id': 'MONITOR'})
        logger.info("🤖 Instance stats (Total since start):", extra={'instance_id': 'MONITOR'})
        total_processed_all = 0
        total_errors_all = 0
        for instance in instances:
            total_processed_all += instance.processed_count
            total_errors_all += instance.error_count
            logger.info(f"  • {instance.instance_id}: {instance.processed_count} processed, {instance.error_count} errors")
        logger.info(f"  • TOTALS: {total_processed_all} processed, {total_errors_all} errors", extra={'instance_id': 'MONITOR'})

        logger.info("=" * 80, extra={'instance_id': 'MONITOR'})

        # Check for completion
        if status['remaining'] == 0 and not found_processing:
            logger.info("🏁 All files processed. Monitor shutting down.", extra={'instance_id': 'MONITOR'})
            break # Exit the monitor loop

async def main():
    """Main function to run multiple instances"""
    print("🎬 AnimePahe Multi-Instance Batch Iframe Extractor (Episode-Track Version)")
    print("=" * 50)
    
    directory = input("Enter directory path (press Enter for current directory): ").strip()
    if not directory: directory = "."
    if not os.path.exists(directory):
        print(f"❌ Directory '{directory}' does not exist!")
        return
    
    num_instances = int(input("Enter number of instances to run (1-8): ").strip() or "3")
    num_instances = max(1, min(8, num_instances))
    
    browsers_per_instance = int(input("Enter browsers per instance (1-4): ").strip() or "2")
    browsers_per_instance = max(1, min(4, browsers_per_instance))
    
    tabs_per_browser = int(input("Enter tabs per browser (1-4): ").strip() or "2")
    tabs_per_browser = max(1, min(4, tabs_per_browser))
    
    total_concurrency = num_instances * browsers_per_instance * tabs_per_browser
    
    print(f"\n🚀 Starting {num_instances} instances...")
    print(f"📁 Directory: {os.path.abspath(directory)}")
    print(f"🔧 Total concurrent operations: {total_concurrency}")
    print("⚡ Resource blocking ENABLED (for speed)")
    print("📝 Failure logging ENABLED (to failed_episodes.jsonl)")
    
    if total_concurrency > 32:
        print(f"\n⚠️ WARNING: {total_concurrency} total operations is very high!")
    
    # Initialize global progress tracker
    global_progress = GlobalProgress()
    failure_logger = FailureLogger()
    
    # --- NEW --- Pre-scan to get total episode count
    coordinator = InstanceCoordinator()
    all_files = coordinator.get_available_files(directory)
    global_progress.total_files = len(all_files)
    
    print(f"📊 Found {global_progress.total_files} files to process.")
    print("🔍 Calculating total episodes... (This may take a moment)")
    
    total_episodes_count = 0
    completed_episodes_count = 0
    for file_path in all_files:
        try:
            # Use sync file reading here since asyncio loop isn't running yet
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for anime in data.get('anime', []):
                    for episode in anime.get('episodes', []):
                        total_episodes_count += 1
                        if episode.get('iframe_url'):
                            completed_episodes_count += 1
        except Exception as e:
            print(f"⚠️ Warning: Could not read {file_path} for episode count: {e}")
    
    global_progress.total_episodes = total_episodes_count
    global_progress.completed_episodes = completed_episodes_count
    
    print(f"📈 Found {global_progress.total_episodes} total episodes.")
    print(f"✅ {global_progress.completed_episodes} episodes already completed.")
    # --- END NEW ---
    
    confirm = input("\n⚠️  This will modify your JSON files. Continue? (y/N): ").strip().lower()
    if confirm not in ['y', 'yes']:
        print("❌ Operation cancelled.")
        return
    
    instances = []
    tasks = []
    
    for i in range(num_instances):
        instance_id = f"INST-{i+1:02d}"
        extractor = MultiInstanceAnimeExtractor(
            instance_id=instance_id,
            max_browsers=browsers_per_instance,
            max_tabs_per_browser=tabs_per_browser,
            global_progress=global_progress,
            failure_logger=failure_logger
        )
        instances.append(extractor)
        task = asyncio.create_task(extractor.run_instance(directory))
        tasks.append(task)
        await asyncio.sleep(5)
    
    monitor_task = asyncio.create_task(progress_monitor(global_progress, instances))
    tasks.append(monitor_task)
    
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print("\n🛑 Shutting down instances...")
        monitor_task.cancel()
    finally:
        for instance in instances:
            await instance.cleanup()
        
        print("\n🎉 All instances stopped!")
        
        total_processed = sum(instance.processed_count for instance in instances)
        total_errors = sum(instance.error_count for instance in instances)
        
        print("=" * 50)
        print("📊 FINAL SUMMARY")
        print("=" * 50)
        print(f"📈 Final Episode Count: {global_progress.completed_episodes} / {global_progress.total_episodes}")
        print(f"✅ Total iframe URLs newly extracted: {total_processed}")
        print(f"❌ Total errors: {total_errors}")
        print(f"📝 Check 'failed_episodes.jsonl' for episodes that could not be processed.")
        
        for instance in instances:
            print(f"📦 {instance.instance_id}: {instance.processed_count} processed, {instance.error_count} errors")

if __name__ == "__main__":
    asyncio.run(main())
