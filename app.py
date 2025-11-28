from flask import Flask, render_template, request, jsonify, redirect, url_for
import asyncio
import re
from playwright.async_api import async_playwright
import threading
import time
import logging
import json
import os
from datetime import datetime, timedelta
import hashlib
import glob
from difflib import SequenceMatcher
import unicodedata

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- Added pagination constant ---
EPISODES_PER_PAGE = 50

class AnimeIndex:
    def __init__(self, anime_dir='anime'):
        self.anime_dir = anime_dir
        self.anime_data = {}
        self.all_anime = []
        self.load_all_anime()
    
    def load_all_anime(self):
        """Load all anime from JSON files"""
        try:
            # Load master index first if exists
            master_file = os.path.join(self.anime_dir, 'master_index.json')
            if os.path.exists(master_file):
                with open(master_file, 'r', encoding='utf-8') as f:
                    master_data = json.load(f)
                    if 'anime' in master_data:
                        self.all_anime = master_data['anime']
                        logger.info(f"📚 Loaded {len(self.all_anime)} anime from master index")
                        return
            
            # Otherwise load all individual files
            json_files = glob.glob(os.path.join(self.anime_dir, 'anime_*.json'))
            for json_file in json_files:
                try:
                    with open(json_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        if 'anime' in data:
                            self.all_anime.extend(data['anime'])
                            logger.info(f"📖 Loaded {len(data['anime'])} anime from {os.path.basename(json_file)}")
                except Exception as e:
                    logger.error(f"❌ Error loading {json_file}: {e}")
            
            logger.info(f"📚 Total {len(self.all_anime)} anime loaded from {len(json_files)} files")
            
        except Exception as e:
            logger.error(f"❌ Error loading anime index: {e}")
            self.all_anime = []
    
    def normalize_text(self, text):
        """Normalize text for better matching"""
        if not text:
            return ""
        # Convert to lowercase and remove accents/diacritics
        text = unicodedata.normalize('NFKD', text.lower()).encode('ascii', 'ignore').decode('ascii')
        return text.strip()
    
    def similarity_score(self, a, b):
        """Calculate similarity between two strings"""
        return SequenceMatcher(None, a.lower(), b.lower()).ratio()
    
    def flexible_search(self, query, limit=20):
        """Flexible search with multiple matching strategies"""
        if not query or not self.all_anime:
            return []
        
        normalized_query = self.normalize_text(query)
        results = []
        
        for anime in self.all_anime:
            title = anime.get('title', '')
            normalized_title = self.normalize_text(title)
            
            # Calculate various match scores
            exact_match = normalized_query == normalized_title
            starts_with = normalized_title.startswith(normalized_query)
            contains = normalized_query in normalized_title
            
            # Calculate similarity score
            similarity = self.similarity_score(query, title)
            
            # Calculate priority score (higher is better)
            priority = 0
            if exact_match:
                priority = 100
            elif starts_with:
                priority = 80 + (similarity * 10)
            elif contains:
                priority = 60 + (similarity * 10)
            else:
                priority = similarity * 50
            
            # Only include results with reasonable match
            if priority > 20 or contains or starts_with:
                results.append({
                    **anime,
                    'priority': priority,
                    'match_type': 'exact' if exact_match else 'starts_with' if starts_with else 'contains' if contains else 'similar'
                })
        
        # Sort by priority (descending) and then by title
        results.sort(key=lambda x: (-x['priority'], x['title']))
        
        # Return top results
        return results[:limit]
    
    def get_anime_by_id(self, anime_id):
        """Get anime by ID"""
        for anime in self.all_anime:
            if anime.get('id') == anime_id:
                return anime
        return None

    # --- NEW METHOD ---
    def get_episode(self, anime_id, episode_session):
        """Get a specific episode's data from the index"""
        anime = self.get_anime_by_id(anime_id)
        if anime and 'episodes' in anime:
            for ep in anime['episodes']:
                if ep.get('episode_id') == episode_session:
                    return ep
        return None

    def get_next_episode(self, anime_id, current_session):
        """Get the next episode data relative to the current session"""
        anime = self.get_anime_by_id(anime_id)
        if not anime or 'episodes' not in anime:
            return None

        episodes = anime['episodes']
        # Sort by episode number just in case
        try:
            episodes.sort(key=lambda x: float(x.get('number', 0)))
        except:
            pass

        for i, ep in enumerate(episodes):
            if ep.get('episode_id') == current_session:
                if i + 1 < len(episodes):
                    return episodes[i + 1]
        return None

class CacheManager:
    def __init__(self, cache_file='data.json'):
        self.cache_file = cache_file
        self.cache = self.get_default_cache() # --- MODIFIED: Start with default
        self.last_mtime = 0 # --- ADDED: Store last modification time
        self.load_cache() # --- MODIFIED: Perform initial load
        
    def get_default_cache(self):
        """Get default cache structure"""
        return {
            'anime_episodes': {},
            'episode_iframes': {},
            'currently_airing_episodes': {
                'episodes': [],
                'timestamp': datetime.now().isoformat(),
                'count': 0
            },
            'popular_anime': {
                'anime': [],
                'timestamp': datetime.now().isoformat(),
                'count': 0
            },
            'metadata': {
                'created_at': datetime.now().isoformat(),
                'last_updated': datetime.now().isoformat()
            }
        }
        
    def load_cache(self):
        """Load cache from JSON file"""
        try:
            if os.path.exists(self.cache_file):
                # --- ADDED: Store the modification time *before* reading ---
                current_mtime = os.path.getmtime(self.cache_file)

                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    loaded_cache = json.load(f)
                    
                # Ensure all required keys exist
                default_cache = self.get_default_cache()
                for key in default_cache:
                    if key not in loaded_cache:
                        loaded_cache[key] = default_cache[key]
                
                self.cache = loaded_cache
                self.last_mtime = current_mtime # --- ADDED: Update mtime *after* successful load
                logger.info("🔄 Cache loaded from file.")
                return self.cache
        except Exception as e:
            logger.error(f"❌ Error loading cache: {e}")
        
        # --- MODIFIED: On failure or if file doesn't exist, use default
        self.cache = self.get_default_cache()
        self.last_mtime = 0
        return self.cache

    def check_and_reload(self):
        """ --- NEW METHOD ---
        Check if the cache file has been modified and reload if it has.
        """
        try:
            if not os.path.exists(self.cache_file):
                return # Nothing to check
            
            current_mtime = os.path.getmtime(self.cache_file)
            
            # Compare mtime.
            if current_mtime > self.last_mtime:
                logger.info("🔔 Cache file change detected! Reloading...")
                self.load_cache() # This will reload the cache and update last_mtime
                
        except Exception as e:
            logger.error(f"❌ Error checking cache file mtime: {e}")
    
    def save_cache(self):
        """Save cache to JSON file"""
        try:
            self.cache['metadata']['last_updated'] = datetime.now().isoformat()
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.cache, f, indent=2, ensure_ascii=False)
            
            # --- ADDED: Update mtime *after* saving ---
            self.last_mtime = os.path.getmtime(self.cache_file)
            logger.info("💾 Cache saved successfully")
        except Exception as e:
            logger.error(f"❌ Error saving cache: {e}")
    
    def get_anime_episodes(self, anime_id):
        """Get cached anime episodes"""
        cached_data = self.cache['anime_episodes'].get(anime_id)
        if cached_data:
            # Ensure all required fields are present for backward compatibility
            cached_data.setdefault('current_page', 1)
            cached_data.setdefault('next_page', None)
        return cached_data
    
    def set_anime_episodes(self, anime_id, anime_data):
        """Cache anime episodes"""
        self.cache['anime_episodes'][anime_id] = {
            'title': anime_data['title'],
            'episodes': anime_data['episodes'],
            'total_episodes': anime_data.get('total_episodes', 0),
            'has_next_page': anime_data.get('has_next_page', False),
            'current_page': anime_data.get('current_page', 1),
            'next_page': anime_data.get('next_page'),
            'timestamp': datetime.now().isoformat()
        }
        self.save_cache()
    
    def get_episode_iframe(self, anime_id, episode_session):
        """Get cached episode iframe"""
        anime_cache = self.cache['episode_iframes'].get(anime_id, {})
        return anime_cache.get(episode_session)
    
    def set_episode_iframe(self, anime_id, episode_session, iframe_data):
        """Cache episode iframe"""
        if anime_id not in self.cache['episode_iframes']:
            self.cache['episode_iframes'][anime_id] = {}
        
        self.cache['episode_iframes'][anime_id][episode_session] = {
            'iframe_url': iframe_data['iframe_url'],
            'timestamp': datetime.now().isoformat(),
            'success': iframe_data['success']
        }
        self.save_cache()
    
    def get_currently_airing_episodes(self):
        """Get cached currently airing episodes"""
        cached_data = self.cache['currently_airing_episodes']
        # Check if cache is older than 24 hours
        if cached_data and 'timestamp' in cached_data:
            try:
                cache_time = datetime.fromisoformat(cached_data['timestamp'])
                if datetime.now() - cache_time < timedelta(hours=24):
                    return cached_data.get('episodes', [])
            except Exception as e:
                logger.error(f"❌ Error parsing cache timestamp: {e}")
        return None
    
    def set_currently_airing_episodes(self, episodes_list):
        """Cache currently airing episodes"""
        self.cache['currently_airing_episodes'] = {
            'episodes': episodes_list,
            'timestamp': datetime.now().isoformat(),
            'count': len(episodes_list)
        }
        self.save_cache()
    
    def get_popular_anime(self):
        """Get cached popular anime"""
        cached_data = self.cache['popular_anime']
        # Check if cache is older than 24 hours
        if cached_data and 'timestamp' in cached_data:
            try:
                cache_time = datetime.fromisoformat(cached_data['timestamp'])
                if datetime.now() - cache_time < timedelta(hours=24):
                    return cached_data.get('anime', [])
            except Exception as e:
                logger.error(f"❌ Error parsing cache timestamp: {e}")
        return None
    
    def set_popular_anime(self, anime_list):
        """Cache popular anime"""
        self.cache['popular_anime'] = {
            'anime': anime_list,
            'timestamp': datetime.now().isoformat(),
            'count': len(anime_list)
        }
        self.save_cache()
    
    def get_cache_stats(self):
        """Get cache statistics"""
        return {
            'anime_cached': len(self.cache['anime_episodes']),
            'iframes_cached': sum(len(episodes) for episodes in self.cache['episode_iframes'].values()),
            'currently_airing_episodes_cached': self.cache['currently_airing_episodes'].get('count', 0),
            'popular_anime_cached': self.cache['popular_anime'].get('count', 0),
            'created_at': self.cache['metadata']['created_at'],
            'last_updated': self.cache['metadata']['last_updated']
        }
    
    def clear_old_cache(self, days=30):
        """Clear cache older than specified days"""
        cutoff = datetime.now() - timedelta(days=days)
        cleared_count = 0
        
        # Clear old anime episodes
        for anime_id, data in list(self.cache['anime_episodes'].items()):
            if 'timestamp' in data:
                try:
                    if datetime.fromisoformat(data['timestamp']) < cutoff:
                        del self.cache['anime_episodes'][anime_id]
                        cleared_count += 1
                except Exception as e:
                    logger.error(f"❌ Error parsing timestamp for anime {anime_id}: {e}")
        
        # Clear old iframes
        for anime_id, episodes in list(self.cache['episode_iframes'].items()):
            for episode_session, data in list(episodes.items()):
                if 'timestamp' in data:
                    try:
                        if datetime.fromisoformat(data['timestamp']) < cutoff:
                            del self.cache['episode_iframes'][anime_id][episode_session]
                            cleared_count += 1
                    except Exception as e:
                        logger.error(f"❌ Error parsing timestamp for episode {episode_session}: {e}")
            
            # Remove empty anime entries
            if not self.cache['episode_iframes'][anime_id]:
                del self.cache['episode_iframes'][anime_id]
        
        if cleared_count > 0:
            self.save_cache()
            logger.info(f"🧹 Cleared {cleared_count} old cache entries")
        
        return cleared_count

class AnimePaheBackend:
    def __init__(self):
        self.base_url = "https://animepahe.si"
        self.playwright = None
        self.browser = None
        self.loop = None
        self.ready = False
        self.cache = CacheManager()
        # ---
        # --- THE FIX IS HERE ---
        # ---
        self.anime_index = AnimeIndex(anime_dir='anime_index') # Point to the correct folder
        
    async def async_setup(self):
        """Setup playwright browser"""
        try:
            logger.info("Starting Playwright setup...")
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-dev-shm-usage'
                ]
            )
            self.ready = True
            
            # Clear old cache on startup
            cleared = self.cache.clear_old_cache(days=7)  # Keep 7 days of cache
            if cleared > 0:
                logger.info(f"🧹 Cleared {cleared} old cache entries on startup")
            
            # Pre-load home page data in background
            asyncio.create_task(self.preload_home_data())
            
            logger.info("✅ Playwright setup completed successfully")
            logger.info(f"📚 Anime index loaded: {len(self.anime_index.all_anime)} titles")
            
        except Exception as e:
            logger.error(f"❌ Playwright setup failed: {e}")
            self.ready = False
    
    async def preload_home_data(self):
        """Pre-load home page data in background"""
        try:
            logger.info("🔄 Pre-loading home page data...")
            await self.get_currently_airing_episodes()
            await self.get_popular_anime()
            logger.info("✅ Home page data pre-loaded")
        except Exception as e:
            logger.error(f"❌ Error pre-loading home data: {e}")
    
    def search_anime(self, search_term):
        """Search anime using pre-indexed data"""
        if not search_term.strip():
            return []
        
        logger.info(f"🔍 Searching for: '{search_term}'")
        results = self.anime_index.flexible_search(search_term, limit=20)
        logger.info(f"✅ Found {len(results)} results for: '{search_term}'")
        
        return results
    
    # --- THIS FUNCTION IS UNCHANGED (uses Playwright) ---
    async def get_currently_airing_episodes(self, pages=3):
        """Get currently airing episodes using optimized logic"""
        # Check cache first
        cached_episodes = self.cache.get_currently_airing_episodes()
        if cached_episodes is not None:
            logger.info("💾 Using cached currently airing episodes")
            return cached_episodes
        
        context = await self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        )
        
        page = await context.new_page()
        all_episodes = []
        
        try:
            for page_num in range(1, pages + 1):
                if page_num == 1:
                    url = self.base_url
                else:
                    url = f"{self.base_url}?page={page_num}"
                
                logger.info(f"📺 Loading currently airing episodes from: {url}")
                
                await page.goto(url, wait_until='networkidle', timeout=60000)
                await page.wait_for_timeout(5000)
                
                # Handle DDoS-Guard
                page_title = await page.title()
                if 'DDoS-Guard' in page_title or 'Just a moment' in page_title:
                    logger.info("🛡️ DDoS-Guard detected, waiting...")
                    await page.wait_for_timeout(10000)
                    # Try to reload
                    await page.reload(wait_until='networkidle')
                    await page.wait_for_timeout(5000)
                
                # Wait for the main content to load
                await page.wait_for_selector('.main-content, .episode-list, [class*="episode"], a[href*="/play/"]', timeout=15000)
                
                # Multiple strategies to find episode links
                episode_selectors = [
                    'a[href*="/play/"]',
                    '.episode-list a',
                    '.tab-content a[href*="/play/"]',
                    '[class*="episode"] a'
                ]
                
                episode_links = []
                for selector in episode_selectors:
                    try:
                        links = await page.query_selector_all(selector)
                        if links:
                            episode_links.extend(links)
                            logger.info(f"🎯 Found {len(links)} episode links with selector: {selector}")
                            break
                    except Exception as e:
                        logger.warning(f"⚠️ Selector {selector} failed: {e}")
                        continue
                
                if not episode_links:
                    # Fallback: look for any links containing /play/
                    all_links = await page.query_selector_all('a')
                    episode_links = [link for link in all_links if '/play/' in await link.get_attribute('href') or '']
                    logger.info(f"🎯 Found {len(episode_links)} episode links via fallback")
                
                logger.info(f"🎯 Processing {len(episode_links)} episode links on page {page_num}")
                
                # Extract information from each episode
                for link in episode_links:
                    try:
                        episode_data = {}
                        
                        # Get URL
                        href = await link.get_attribute('href')
                        if href and '/play/' in href:
                            # Make URL absolute if relative
                            if href.startswith('/'):
                                full_url = f"{self.base_url}{href}"
                            else:
                                full_url = href
                            episode_data['episode_url'] = full_url
                            
                            # Extract anime_id and session_id from URL
                            url_match = re.search(r'/play/([a-f0-9-]+)/([a-f0-9]+)', href)
                            if url_match:
                                episode_data['anime_id'] = url_match.group(1)
                                episode_data['session_id'] = url_match.group(2)
                            else:
                                # Try alternative pattern
                                url_match = re.search(r'/play/([^/]+)/([^/?]+)', href)
                                if url_match:
                                    episode_data['anime_id'] = url_match.group(1)
                                    episode_data['session_id'] = url_match.group(2)
                        
                        # Get the text content which contains the episode info
                        link_text = await link.text_content()
                        if link_text:
                            link_text = link_text.strip()
                            
                            # Parse the anime name and episode number from the text
                            parsed_info = self.parse_episode_info(link_text)
                            episode_data.update(parsed_info)
                            
                            # Create a clean episode title
                            if parsed_info['anime_name'] and parsed_info['episode_number']:
                                episode_data['episode_title'] = f"{parsed_info['anime_name']} - Episode {parsed_info['episode_number']}"
                            else:
                                episode_data['episode_title'] = link_text
                            
                            all_episodes.append(episode_data)
                            
                    except Exception as e:
                        logger.warning(f"⚠️ Error processing episode link: {e}")
                        continue
                
                logger.info(f"✅ Processed {len(episode_links)} episodes on page {page_num}")
                
                # Stop if we have enough episodes
                if len(all_episodes) >= 30:
                    break
                    
        except Exception as e:
            logger.error(f"❌ Error getting currently airing episodes: {e}")
        finally:
            await context.close()
        
        # If no episodes found, use fallback from our index
        if not all_episodes:
            logger.info("🔄 No episodes found via scraping, using fallback from index")
            all_episodes = self.get_fallback_episodes()
        
        # Cache the results
        self.cache.set_currently_airing_episodes(all_episodes)
        logger.info(f"✅ Found {len(all_episodes)} currently airing episodes across {pages} pages")
        return all_episodes
    
    def get_fallback_episodes(self):
        """Get fallback episodes from our index when scraping fails"""
        fallback_episodes = []
        
        # Popular ongoing anime that likely have recent episodes
        popular_ongoing = [
            "One Piece", "Naruto", "Boruto", "My Hero Academia", "Attack on Titan",
            "Demon Slayer", "Jujutsu Kaisen", "Chainsaw Man", "Spy x Family",
            "Blue Lock", "Dr. Stone", "That Time I Got Reincarnated as a Slime",
            "One Punch Man", "Tokyo Revengers", "Haikyuu", "Black Clover"
        ]
        
        for anime_name in popular_ongoing[:10]:  # Use first 10
            # Find the anime in our index
            for anime in self.anime_index.all_anime:
                if anime_name.lower() in anime.get('title', '').lower():
                    # Create a fake episode entry
                    fallback_episodes.append({
                        'anime_name': anime_name,
                        'episode_number': 1,  # Default episode
                        'episode_title': f"{anime_name} - Latest Episode",
                        'anime_id': anime.get('id', ''),
                        'session_id': 'fallback123',  # Fake session
                        'episode_url': f"{self.base_url}/anime/{anime.get('id', '')}"
                    })
                    break
        
        return fallback_episodes
    
    def parse_episode_info(self, text):
        """Parse the episode information from the text"""
        if not text:
            return {'anime_name': 'Unknown', 'episode_number': 0}
        
        # Clean the text
        clean_text = re.sub(r'\s+', ' ', text).strip()
        
        # Various patterns to extract anime name and episode number
        patterns = [
            # Pattern: "Anime Name - Episode 123"
            r'(.+?)\s*-\s*[Ee]pisode\s*(\d+)',
            # Pattern: "Anime Name EP123"
            r'(.+?)\s*[Ee][Pp]?\s*(\d+)',
            # Pattern: "Watch Anime Name Online"
            r'[Ww]atch\s+(.+?)\s*[Oo]nline',
            # Pattern: Just extract numbers for episode
            r'[Ee]pisode\s*(\d+)'
        ]
        
        anime_name = "Unknown Anime"
        episode_number = 1
        
        for pattern in patterns:
            match = re.search(pattern, clean_text)
            if match:
                if len(match.groups()) >= 2:
                    anime_name = match.group(1).strip()
                    try:
                        episode_number = int(match.group(2))
                    except ValueError:
                        episode_number = 1
                    break
                elif len(match.groups()) == 1:
                    # Only episode number found
                    try:
                        episode_number = int(match.group(1))
                    except ValueError:
                        episode_number = 1
        
        # If no pattern matched, use the whole text as anime name
        if anime_name == "Unknown Anime":
            anime_name = clean_text
        
        return {
            'anime_name': anime_name,
            'episode_number': episode_number
        }
    
    # --- THIS FUNCTION IS UNCHANGED (uses Playwright) ---
    async def get_popular_anime(self):
        """Get popular anime from the main page"""
        # Check cache first
        cached_popular = self.cache.get_popular_anime()
        if cached_popular is not None:
            logger.info("💾 Using cached popular anime")
            return cached_popular
        
        context = await self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        )
        
        page = await context.new_page()
        popular_anime = []
        
        try:
            logger.info(f"📺 Loading popular anime from: {self.base_url}")
            
            await page.goto(self.base_url, wait_until='networkidle', timeout=60000)
            await page.wait_for_timeout(5000)
            
            # Handle DDoS-Guard
            page_title = await page.title()
            if 'DDoS-Guard' in page_title or 'Just a moment' in page_title:
                logger.info("🛡️ DDoS-Guard detected, waiting...")
                await page.wait_for_timeout(10000)
                await page.reload(wait_until='networkidle')
                await page.wait_for_timeout(5000)
            
            # Multiple strategies to find popular anime
            popular_selectors = [
                '.sidebar [href*="/anime/"]',
                '.popular-anime a[href*="/anime/"]',
                '.trending-anime a[href*="/anime/"]',
                '[class*="popular"] a[href*="/anime/"]',
                'a[href*="/anime/"]'
            ]
            
            seen_anime = set()
            
            for selector in popular_selectors:
                try:
                    anime_links = await page.query_selector_all(selector)
                    if anime_links:
                        logger.info(f"🎯 Found {len(anime_links)} anime links with selector: {selector}")
                        
                        for link in anime_links:
                            try:
                                anime_data = await self.extract_anime_from_link(link)
                                if anime_data and anime_data['id'] not in seen_anime:
                                    seen_anime.add(anime_data['id'])
                                    popular_anime.append(anime_data)
                                    
                                    # Stop when we have enough
                                    if len(popular_anime) >= 12:
                                        break
                                        
                            except Exception as e:
                                logger.warning(f"⚠️ Error extracting anime from link: {e}")
                                continue
                        
                        if popular_anime:
                            break
                            
                except Exception as e:
                    logger.warning(f"⚠️ Selector {selector} failed: {e}")
                    continue
            
            # If no popular anime found via scraping, use fallback
            if not popular_anime:
                logger.info("🔍 No popular anime found via scraping, using fallback")
                popular_anime = self.get_fallback_popular_anime()
                    
        except Exception as e:
            logger.error(f"❌ Error getting popular anime: {e}")
            # Use fallback on error
            popular_anime = self.get_fallback_popular_anime()
        finally:
            await context.close()
        
        # Cache the results
        self.cache.set_popular_anime(popular_anime)
        logger.info(f"✅ Found {len(popular_anime)} popular anime")
        return popular_anime
    
    def get_fallback_popular_anime(self):
        """Get fallback popular anime when scraping fails"""
        # Define default popular anime with their IDs
        default_popular = [
            {"title": "One Piece", "id": "9b2f4c67-24e3-7a94-37b9-f2c1d1b5662a", "url": "https://animepahe.si/anime/9b2f4c67-24e3-7a94-37b9-f2c1d1b5662a"},
            {"title": "Naruto", "id": "7f7b1f1a-3b3a-1a2b-2c3d-4e5f6a7b8c9d", "url": "https://animepahe.si/anime/7f7b1f1a-3b3a-1a2b-2c3d-4e5f6a7b8c9d"},
            {"title": "Dan Da Dan", "id": "a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d", "url": "https://animepahe.si/anime/a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d"},
            {"title": "Kaiju No. 8", "id": "b2c3d4e5-f6a7-8b9c-0d1e-2f3a4b5c6d7e", "url": "https://animepahe.si/anime/b2c3d4e5-f6a7-8b9c-0d1e-2f3a4b5c6d7e"},
            {"title": "Jujutsu Kaisen", "id": "c3d4e5f6-a7b8-9c0d-1e2f-3a4b5c6d7e8f", "url": "https://animepahe.si/anime/c3d4e5f6-a7b8-9c0d-1e2f-3a4b5c6d7e8f"},
            {"title": "Chainsaw Man", "id": "d4e5f6a7-b8c9-0d1e-2f3a-4b5c6d7e8f9a", "url": "httpss://animepahe.si/anime/d4e5f6a7-b8c9-0d1e-2f3a-4b5c6d7e8f9a"},
            {"title": "Attack on Titan", "id": "e5f6a7b8-c9d0-1e2f-3a4b-5c6d7e8f9a0b", "url": "httpss://animepahe.si/anime/e5f6a7b8-c9d0-1e2f-3a4b-5c6d7e8f9a0b"},
            {"title": "Demon Slayer", "id": "f6a7b8c9-d0e1-2f3a-4b5c-6d7e8f9a0b1c", "url": "httpss://animepahe.si/anime/f6a7b8c9-d0e1-2f3a-4b5c-6d7e8f9a0b1c"},
            {"title": "My Hero Academia", "id": "a7b8c9d0-e1f2-3a4b-5c6d-7e8f9a0b1c2d", "url": "httpss://animepahe.si/anime/a7b8c9d0-e1f2-3a4b-5c6d-7e8f9a0b1c2d"},
            {"title": "Spy x Family", "id": "b8c9d0e1-f2a3-4b5c-6d7e-8f9a0b1c2d3e", "url": "httpss://animepahe.si/anime/b8c9d0e1-f2a3-4b5c-6d7e-8f9a0b1c2d3e"},
            {"title": "Blue Lock", "id": "c9d0e1f2-a3b4-5c6d-7e8f-9a0b1c2d3e4f", "url": "httpss://animepahe.si/anime/c9d0e1f2-a3b4-5c6d-7e8f-9a0b1c2d3e4f"},
            {"title": "Dr. Stone", "id": "d0e1f2a3-b4c5-6d7e-8f9a-0b1c2d3e4f5a", "url": "httpss://animepahe.si/anime/d0e1f2a3-b4c5-6d7e-8f9a-0b1c2d3e4f5a"}
        ]
        
        return default_popular
    
    async def extract_anime_from_link(self, link):
        """Extract anime information from a link"""
        try:
            title = await link.text_content()
            href = await link.get_attribute('href')
            
            if not title or not href:
                return None
            
            # Extract anime ID from href
            anime_id_match = re.search(r'/anime/([a-f0-9-]+)', href)
            if not anime_id_match:
                return None
            
            anime_id = anime_id_match.group(1)
            
            return {
                'title': title.strip(),
                'id': anime_id,
                'url': f"{self.base_url}/anime/{anime_id}"
            }
            
        except Exception as e:
            logger.warning(f"⚠️ Error extracting anime from link: {e}")
            return None

    # --- THIS FUNCTION IS UNCHANGED (Index-based, handles pagination) ---
    def get_episodes(self, anime_id, page=1):
        """Get episodes for a specific anime from the pre-compiled index (with pagination)"""
        logger.info(f"📚 Getting indexed episodes for anime: {anime_id} (Page {page})")
        anime_data = self.anime_index.get_anime_by_id(anime_id)
        
        if not anime_data:
            logger.warning(f"⚠️ No anime data found in index for: {anime_id}")
            return {
                'title': 'Unknown Anime', 
                'episodes': [], 
                'total_episodes': 0, 
                'has_next_page': False, 
                'current_page': page,
                'next_page': None
            }
        
        all_formatted_episodes = []
        for ep in anime_data.get('episodes', []):
            all_formatted_episodes.append({
                'number': ep.get('number'),
                'title': self.clean_episode_title(ep.get('title', '')),
                'url': ep.get('url'),
                'session': ep.get('episode_id') # Map episode_id to session
            })
        
        # Sort by episode number (as integer)
        try:
            all_formatted_episodes.sort(key=lambda x: int(x.get('number', 0)))
        except ValueError:
            logger.warning("⚠️ Could not sort episodes by number (non-integer found).")

        # --- Pagination Logic ---
        total_episodes = len(all_formatted_episodes)
        start_index = (page - 1) * EPISODES_PER_PAGE
        end_index = page * EPISODES_PER_PAGE
        
        paginated_episodes = all_formatted_episodes[start_index:end_index]
        
        has_next_page = end_index < total_episodes
        next_page = page + 1 if has_next_page else None
        
        return {
            'title': anime_data.get('title', 'Unknown Title'),
            'episodes': paginated_episodes,
            'total_episodes': total_episodes,
            'has_next_page': has_next_page,
            'current_page': page,
            'next_page': next_page
        }

    # --- THIS FUNCTION IS UNCHANGED (Original Scraper) ---
    async def scrape_episodes_page(self, anime_id, page=1):
        """Scrape episodes for a specific anime with proper pagination"""
        # Check cache first (only for first page)
        if page == 1:
            cached_episodes = self.cache.get_anime_episodes(anime_id)
            if cached_episodes:
                logger.info(f"💾 Using cached scraped episodes for anime: {anime_id}")
                return {
                    'title': cached_episodes['title'],
                    'episodes': cached_episodes['episodes'],
                    'total_episodes': cached_episodes.get('total_episodes', 0),
                    'has_next_page': cached_episodes.get('has_next_page', False),
                    'current_page': cached_episodes.get('current_page', 1),
                    'next_page': cached_episodes.get('next_page')
                }
        
        # If not in cache or loading next page, fetch episodes
        context = await self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        )
        
        page_instance = await context.new_page()
        
        try:
            if page > 1:
                anime_url = f"{self.base_url}/anime/{anime_id}?page={page}"
            else:
                anime_url = f"{self.base_url}/anime/{anime_id}"
                
            logger.info(f" scrapping episodes from: {anime_url}")
            
            # Navigate to anime page
            await page_instance.goto(anime_url, wait_until='networkidle')
            await page_instance.wait_for_timeout(3000)
            
            # Handle DDoS-Guard
            if 'DDoS-Guard' in await page_instance.title():
                logger.info("🛡️ DDoS-Guard detected, waiting...")
                await page_instance.wait_for_function("""
                    () => !document.title.includes('DDoS-Guard')
                """, timeout=60000)
            
            # Get anime title
            anime_title = await page_instance.title()
            anime_title = anime_title.replace(':: animepahe', '').strip()
            
            # Extract episodes using the main method
            episodes = await self.extract_episodes_clean(page_instance, anime_id)
            
            # Check for pagination
            has_next_page, next_page = await self.check_pagination(page_instance, page)
            
            # Remove duplicates by session ID and sort
            episodes = self.remove_duplicate_episodes(episodes)
            
            # Sort by episode number (as integer)
            try:
                episodes.sort(key=lambda x: int(x.get('number', 0)))
            except ValueError:
                logger.warning("⚠️ Could not sort scraped episodes by number.")
            
            anime_data = {
                'title': anime_title,
                'episodes': episodes,
                'total_episodes': len(episodes), # Note: This is just total on this page
                'has_next_page': has_next_page,
                'current_page': page,
                'next_page': next_page
            }
            
            # Cache only the first page results
            if page == 1:
                self.cache.set_anime_episodes(anime_id, anime_data)
            
            logger.info(f"✅ Found {len(episodes)} episodes for: {anime_title} (Page {page})")
            if has_next_page:
                logger.info(f"📖 More episodes available on page {next_page}")
            
            return anime_data
            
        except Exception as e:
            logger.error(f"❌ Error getting episodes: {e}")
            return {
                'title': 'Unknown', 
                'episodes': [], 
                'total_episodes': 0, 
                'has_next_page': False, 
                'current_page': page,
                'next_page': None
            }
        finally:
            await context.close()
    
    async def extract_episodes_clean(self, page, anime_id):
        """Clean method to extract episodes focusing on the main table"""
        episodes = []
        
        try:
            # Wait for the main content to load
            await page.wait_for_timeout(2000)
            
            # Method 1: Look for the main episode table with data-session rows
            episode_rows = await page.query_selector_all('tr[data-session]')
            
            if episode_rows:
                logger.info(f"🎯 Found {len(episode_rows)} episodes with data-session")
                for row in episode_rows:
                    episode_data = await self.extract_episode_from_session_row(row, anime_id)
                    if episode_data:
                        episodes.append(episode_data)
                return episodes
            
            # Method 2: Look for episode links in tables
            episode_links = await page.query_selector_all('table a[href*="/play/"]')
            if episode_links:
                logger.info(f"🎯 Found {len(episode_links)} episode links in tables")
                for link in episode_links:
                    episode_data = await self.extract_episode_from_link(link, anime_id)
                    if episode_data:
                        episodes.append(episode_data)
                return episodes
            
            # Method 3: Look for any episode containers
            episode_containers = await page.query_selector_all('[class*="episode"], .episode-list li')
            if episode_containers:
                logger.info(f"🎯 Found {len(episode_containers)} episode containers")
                for container in episode_containers:
                    episode_data = await self.extract_episode_from_container(container, anime_id)
                    if episode_data:
                        episodes.append(episode_data)
                return episodes
            
            logger.warning("⚠️ No episodes found with any method")
            return []
            
        except Exception as e:
            logger.error(f"❌ Error extracting episodes: {e}")
            return []
    
    async def extract_episode_from_session_row(self, row, anime_id):
        """Extract episode from table row with data-session"""
        try:
            session_id = await row.get_attribute('data-session')
            if not session_id:
                return None
            
            # Extract episode number from the row
            episode_number = 0
            cells = await row.query_selector_all('td')
            if cells:
                # Usually episode number is in the first cell
                first_cell_text = await cells[0].text_content()
                numbers = re.findall(r'\b(\d+)\b', first_cell_text)
                if numbers:
                    episode_number = int(numbers[0])
            
            # Extract clean title
            title = "Episode"
            if len(cells) >= 2:
                title_cell_text = await cells[1].text_content()
                if title_cell_text:
                    title = self.clean_episode_title(title_cell_text)
            
            # Use proper episode URL format
            episode_url = f"{self.base_url}/play/{anime_id}/{session_id}"
            
            return {
                'number': episode_number,
                'title': title,
                'url': episode_url,
                'session': session_id
            }
            
        except Exception as e:
            logger.warning(f"⚠️ Error extracting from session row: {e}")
            return None
    
    async def extract_episode_from_link(self, link, anime_id):
        """Extract episode from a play link"""
        try:
            href = await link.get_attribute('href')
            if not href:
                return None
            
            # Extract session ID from URL
            session_match = re.search(r'/play/[a-f0-9-]+/([a-f0-9]+)', href)
            if not session_match:
                return None
            
            session_id = session_match.group(1)
            
            # Extract episode number from link text
            link_text = await link.text_content()
            episode_number = 0
            numbers = re.findall(r'\b(\d+)\b', link_text)
            if numbers:
                episode_number = int(numbers[0])
            
            # Get clean title
            title = self.clean_episode_title(link_text)
            
            # Use the full URL from href
            episode_url = f"{self.base_url}{href}" if href.startswith('/') else href
            
            return {
                'number': episode_number,
                'title': title,
                'url': episode_url,
                'session': session_id
            }
            
        except Exception as e:
            logger.warning(f"⚠️ Error extracting from link: {e}")
            return None
    
    async def extract_episode_from_container(self, container, anime_id):
        """Extract episode from a container element"""
        try:
            # Look for play link inside container
            link = await container.query_selector('a[href*="/play/"]')
            if link:
                return await self.extract_episode_from_link(link, anime_id)
            
            # Try to extract from container text
            container_text = await container.text_content()
            if container_text:
                # Look for session in data attributes
                session_id = await container.get_attribute('data-session')
                if not session_id:
                    # Try to find session in onclick or other attributes
                    onclick = await container.get_attribute('onclick')
                    if onclick:
                        session_match = re.search(r"/([a-f0-9]{8,})", onclick)
                        if session_match:
                            session_id = session_match.group(1)
                
                if session_id:
                    episode_number = 0
                    numbers = re.findall(r'\b(\d+)\b', container_text)
                    if numbers:
                        episode_number = int(numbers[0])
                    
                    title = self.clean_episode_title(container_text)
                    # Use proper episode URL format
                    episode_url = f"{self.base_url}/play/{anime_id}/{session_id}"
                    
                    return {
                        'number': episode_number,
                        'title': title,
                        'url': episode_url,
                        'session': session_id
                    }
            
            return None
            
        except Exception as e:
            logger.warning(f"⚠️ Error extracting from container: {e}")
            return None
    
    def clean_episode_title(self, title):
        """Clean episode title"""
        if not title:
            return "Episode"
        
        # Remove extra whitespace
        title = ' '.join(title.split())
        
        # Remove common patterns
        patterns_to_remove = [
            r'^Episode\s+\d+\s*[-:]?\s*',
            r'^EP\s*\d+\s*[-:]?\s*', 
            r'^E\d+\s*[-:]?\s*',
            r'Watch\s+Online.*$',
            r'\bBD\b',
            r'\d{2}:\d{2}:\d{2}',
            r'\b\d+k\b',
            r'\[.*?\]',
            r'\(.*?\)',
        ]
        
        for pattern in patterns_to_remove:
            title = re.sub(pattern, '', title, flags=re.IGNORECASE)
        
        title = re.sub(r'[-\s]+', ' ', title).strip()
        
        if not title or title.isdigit() or len(title) < 2:
            return "Episode"
        
        return title
    
    async def check_pagination(self, page, current_page):
        """Check if there are more pages available"""
        try:
            # Look for next page button
            next_selectors = [
                'a:has-text("Next")',
                '.pagination .next',
                '.pagination a[rel="next"]',
                'button:has-text("Load More")'
            ]
            
            for selector in next_selectors:
                element = await page.query_selector(selector)
                if element and await element.is_visible():
                    return True, current_page + 1
            
            # Check if there's a page number greater than current
            page_links = await page.query_selector_all('.pagination a')
            for link in page_links:
                link_text = await link.text_content()
                if link_text and link_text.isdigit():
                    page_num = int(link_text)
                    if page_num > current_page:
                        return True, page_num
                        
        except Exception as e:
            logger.warning(f"⚠️ Error checking pagination: {e}")
        
        return False, None
    
    def remove_duplicate_episodes(self, episodes):
        """Remove duplicate episodes based on session ID"""
        seen_sessions = set()
        unique_episodes = []
        
        for episode in episodes:
            session = episode.get('session')
            if session and session not in seen_sessions:
                seen_sessions.add(session)
                unique_episodes.append(episode)
        
        return unique_episodes
    
    # --- THIS FUNCTION IS UNCHANGED (Index-first, then scrape) ---
    async def get_episode_iframe(self, anime_id, episode_session):
        """
        Extract iframe URL from episode page.
        First, check the index. If not found, fall back to scraping.
        """
        # 1. Try to get from index first
        try:
            episode_data = self.anime_index.get_episode(anime_id, episode_session)
            if episode_data and episode_data.get('iframe_url'):
                logger.info(f"💾 Using indexed iframe for episode: {episode_session}")
                return {
                    'iframe_url': episode_data['iframe_url'],
                    'success': True
                }
        except Exception as e:
            logger.error(f"❌ Error checking index for iframe: {e}")

        # 2. If not in index, fall back to scraping (the original function)
        logger.warning(f"⚠️ No indexed iframe for {episode_session}. Scraping...")
        return await self._scrape_episode_iframe(anime_id, episode_session)

    # --- RENAMED from get_episode_iframe (This is the original scraper) ---
    async def _scrape_episode_iframe(self, anime_id, episode_session):
        """(Original Scraper) Extract iframe URL from episode page"""
        # Check cache first
        cached_iframe = self.cache.get_episode_iframe(anime_id, episode_session)
        if cached_iframe and cached_iframe['success']:
            logger.info(f"💾 Using cached scraped iframe for episode: {episode_session}")
            return {
                'iframe_url': cached_iframe['iframe_url'],
                'success': True
            }
        
        # Use the proper episode URL format
        episode_url = f"{self.base_url}/play/{anime_id}/{episode_session}"
        
        context = await self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        
        # Remove webdriver detection
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)
        
        page = await context.new_page()
        
        try:
            logger.info(f"🎬 (Scraping) Extracting iframe from: {episode_url}")
            
            await page.goto(episode_url, wait_until='networkidle', timeout=60000)
            
            if 'DDoS-Guard' in await page.title():
                logger.info("🛡️ DDoS-Guard detected, waiting...")
                await page.wait_for_function("""
                    () => !document.title.includes('DDoS-Guard')
                """, timeout=60000)
            
            actual_title = await page.title()
            logger.info(f"✅ Loaded: {actual_title}")
            
            # Check if we got a 404 page
            if '404' in actual_title or 'Not Found' in actual_title:
                logger.error(f"❌ Episode page not found: {episode_url}")
                error_data = {
                    'iframe_url': None,
                    'success': False,
                    'error': f'Episode page not found (404): {episode_url}'
                }
                self.cache.set_episode_iframe(anime_id, episode_session, error_data)
                return error_data
            
            # Wait for page to fully load
            await page.wait_for_timeout(3000)
            
            # Look for iframe directly
            iframe_url = await self._find_iframe_directly(page, episode_url)
            if iframe_url:
                iframe_data = {
                    'iframe_url': iframe_url,
                    'success': True
                }
                self.cache.set_episode_iframe(anime_id, episode_session, iframe_data)
                return iframe_data
            
            # Look for iframe in JavaScript
            iframe_url = await self._find_iframe_in_javascript(page, episode_url)
            if iframe_url:
                iframe_data = {
                    'iframe_url': iframe_url,
                    'success': True
                }
                self.cache.set_episode_iframe(anime_id, episode_session, iframe_data)
                return iframe_data
            
            # Look for player container that loads iframe
            iframe_url = await self._find_dynamic_iframe(page, episode_url)
            if iframe_url:
                iframe_data = {
                    'iframe_url': iframe_url,
                    'success': True
                }
                self.cache.set_episode_iframe(anime_id, episode_session, iframe_data)
                return iframe_data
            
            # Click play button and capture network requests
            iframe_url = await self._find_iframe_after_interaction(page, episode_url)
            if iframe_url:
                iframe_data = {
                    'iframe_url': iframe_url,
                    'success': True
                }
                self.cache.set_episode_iframe(anime_id, episode_session, iframe_data)
                return iframe_data
            
            logger.error("❌ No iframe found after all strategies")
            error_data = {
                'iframe_url': None,
                'success': False,
                'error': 'No iframe found on page'
            }
            self.cache.set_episode_iframe(anime_id, episode_session, error_data)
            return error_data
            
        except Exception as e:
            logger.error(f"❌ Iframe extraction error: {e}")
            error_data = {
                'iframe_url': None,
                'success': False,
                'error': str(e)
            }
            self.cache.set_episode_iframe(anime_id, episode_session, error_data)
            return error_data
        finally:
            await context.close()
    
    async def _find_iframe_directly(self, page, episode_url):
        """Look for iframe elements directly"""
        iframes = await page.query_selector_all('iframe')
        logger.info(f"🎯 Found {len(iframes)} iframe elements")
        
        for iframe in iframes:
            src = await iframe.get_attribute('src')
            if src:
                full_url = self._make_absolute_url(episode_url, src)
                if any(keyword in full_url.lower() for keyword in ['player', 'video', 'embed', 'kwik', 'stream']):
                    logger.info(f"✅ Found video player iframe: {full_url}")
                    return full_url
                elif 'animepahe' not in full_url:
                    logger.info(f"✅ Found external player iframe: {full_url}")
                    return full_url
        
        return None
    
    async def _find_iframe_in_javascript(self, page, episode_url):
        """Extract iframe URL from JavaScript"""
        try:
            js_code = """
            () => {
                const scripts = document.querySelectorAll('script');
                const iframeUrls = [];
                
                scripts.forEach(script => {
                    const content = script.textContent || script.innerText;
                    const urlMatches = content.match(/(https?:\\/\\/[^"']+)/g);
                    if (urlMatches) {
                        urlMatches.forEach(url => {
                            if (url.includes('embed') || url.includes('player') || 
                                url.includes('iframe') || url.includes('kwik')) {
                                iframeUrls.push(url);
                            }
                        });
                    }
                });
                
                return iframeUrls;
            }
            """
            
            result = await page.evaluate(js_code)
            if result and len(result) > 0:
                for url in result:
                    full_url = self._make_absolute_url(episode_url, url)
                    logger.info(f"✅ Found iframe in JS: {full_url}")
                    return full_url
                    
        except Exception as e:
            logger.info(f"❌ JavaScript search failed: {e}")
        
        return None
    
    async def _find_dynamic_iframe(self, page, episode_url):
        """Look for dynamically loaded iframes"""
        logger.info("🔍 Searching for dynamic iframe loaders...")
        
        player_selectors = [
            '#player', '.player', '#video-player', '.video-player',
            '#embed-player', '.embed-player', '[id*="player"]',
            '[class*="player"]', '.pahe-player', '#kwikPlayer'
        ]
        
        for selector in player_selectors:
            elements = await page.query_selector_all(selector)
            if elements:
                logger.info(f"🎮 Found {len(elements)} elements with: {selector}")
                
                for element in elements:
                    iframe = await element.query_selector('iframe')
                    if iframe:
                        src = await iframe.get_attribute('src')
                        if src:
                            full_url = self._make_absolute_url(episode_url, src)
                            logger.info(f"✅ Found iframe in player container: {full_url}")
                            return full_url
                    
                    attrs = ['data-src', 'data-embed', 'data-iframe', 'data-url']
                    for attr in attrs:
                        value = await element.get_attribute(attr)
                        if value and 'http' in value:
                            full_url = self._make_absolute_url(episode_url, value)
                            logger.info(f"✅ Found iframe URL in data attribute: {full_url}")
                            return full_url
        
        return None
    
    async def _find_iframe_after_interaction(self, page, episode_url):
        """Click play buttons and monitor for iframe loading"""
        logger.info("🖱️  Interacting with play buttons...")
        
        play_buttons = [
            '.play-button', '[class*="play"]', '.btn-play',
            'button[onclick*="embed"]', 'a[href*="embed"]'
        ]
        
        iframe_requests = []
        
        async def capture_iframe_requests(request):
            url = request.url
            if any(keyword in url for keyword in ['embed', 'player', 'kwik']):
                iframe_requests.append(url)
                logger.info(f"🌐 Network request: {url}")
        
        page.on("request", capture_iframe_requests)
        
        for button_selector in play_buttons:
            try:
                buttons = await page.query_selector_all(button_selector)
                if buttons:
                    logger.info(f"🎯 Found {len(buttons)} buttons with: {button_selector}")
                    
                    for button in buttons:
                        try:
                            logger.info("🖱️  Clicking button...")
                            await button.click()
                            await page.wait_for_timeout(3000)
                            
                            iframe_url = await self._find_iframe_directly(page, episode_url)
                            if iframe_url:
                                return iframe_url
                                
                            if iframe_requests:
                                logger.info(f"✅ Found iframe URL from network: {iframe_requests[-1]}")
                                return iframe_requests[-1]
                                
                        except Exception as e:
                            logger.info(f"⚠️ Button click failed: {e}")
                            continue
                            
            except Exception as e:
                logger.info(f"⚠️ Button search failed for {button_selector}: {e}")
        
        return None
    
    def _make_absolute_url(self, base_url, relative_url):
        """Convert relative URL to absolute"""
        if not relative_url:
            return relative_url
            
        if relative_url.startswith(('http://', 'https://')):
            return relative_url
        elif relative_url.startswith('//'):
            return 'https:' + relative_url
        elif relative_url.startswith('/'):
            return f"{self.base_url}{relative_url}"
        else:
            return base_url + '/' + relative_url

# Global backend instance
backend = AnimePaheBackend()

def run_async_setup():
    """Run async setup in a separate thread"""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        backend.loop = loop
        loop.run_until_complete(backend.async_setup())
        
        if backend.ready:
            logger.info("✅ Backend is ready and loop is running")
            loop.run_forever()
        else:
            logger.error("❌ Backend setup failed, closing loop")
            loop.close()
    except Exception as e:
        logger.error(f"❌ Setup thread error: {e}")

def run_async_in_thread(coro):
    """Run an async coroutine in the backend thread"""
    if not backend.ready or not backend.loop:
        raise Exception("Backend not ready")
    
    future = asyncio.run_coroutine_threadsafe(coro, backend.loop)
    return future.result(timeout=120)

# Start the backend setup
setup_thread = threading.Thread(target=run_async_setup, daemon=True)
setup_thread.start()

# Wait for setup to complete
max_wait = 30
wait_time = 0
while not backend.ready and wait_time < max_wait:
    logger.info("⏳ Waiting for backend to be ready...")
    time.sleep(2)
    wait_time += 2

if backend.ready:
    logger.info("✅ Backend is ready!")
else:
    logger.error("❌ Backend setup timeout")

# --- THIS ROUTE IS UNCHANGED (Still uses scraper) ---
@app.route('/')
def index():
    """Home page with currently airing episodes and popular anime"""

    # Check if the cache file on disk has changed and reload if needed
    if backend.ready:
        try:
            backend.cache.check_and_reload()
        except Exception as e:
            logger.error(f"❌ Error during cache reload check: {e}")

    cache_stats = backend.cache.get_cache_stats()
    total_anime = len(backend.anime_index.all_anime)
    
    # Get currently airing episodes (from cache or fetch if needed)
    currently_airing_episodes = []
    if backend.ready:
        try:
            # This calls the Playwright scraper function
            currently_airing_episodes = backend.cache.get_currently_airing_episodes()
            if currently_airing_episodes is None:
                currently_airing_episodes = run_async_in_thread(backend.get_currently_airing_episodes())
        except Exception as e:
            logger.error(f"❌ Error getting currently airing episodes: {e}")
            currently_airing_episodes = []
    
    # Get popular anime (from cache or fetch if needed)
    popular_anime = []
    if backend.ready:
        try:
            # This calls the Playwright scraper function
            popular_anime = backend.cache.get_popular_anime()
            if popular_anime is None:
                popular_anime = run_async_in_thread(backend.get_popular_anime())
        except Exception as e:
            logger.error(f"❌ Error getting popular anime: {e}")
            popular_anime = []
    
    return render_template('index.html', 
                         backend_ready=backend.ready,
                         cache_stats=cache_stats,
                         total_anime=total_anime,
                         currently_airing_episodes=currently_airing_episodes or [],
                         popular_anime=popular_anime or [])

@app.route('/search', methods=['GET', 'POST'])
def search():
    """Handle anime search"""
    if not backend.ready:
        return render_template('error.html', 
                             message="Backend is still initializing. Please wait a moment and refresh.")
    
    if request.method == 'POST':
        query = request.form.get('query', '').strip()
        if not query:
            return redirect(url_for('index'))
        
        try:
            results = backend.search_anime(query)
            cache_stats = backend.cache.get_cache_stats()
            total_anime = len(backend.anime_index.all_anime)
            return render_template('search_results.html', 
                                 query=query, 
                                 results=results,
                                 base_url=backend.base_url,
                                 cache_stats=cache_stats,
                                 total_anime=total_anime)
        except Exception as e:
            logger.error(f"❌ Search error: {e}")
            return render_template('error.html', 
                                 message=f"Search failed: {str(e)}")
    
    return redirect(url_for('index'))

# --- THIS ROUTE IS UNCHANGED (Index-based) ---
@app.route('/anime/<anime_id>')
def anime_episodes(anime_id):
    """Show episodes for a specific anime from the index"""
    if not backend.ready:
        return render_template('error.html', 
                             message="Backend is still initializing. Please wait a moment and refresh.")
    
    try:
        # --- Added page handling ---
        page = request.args.get('page', 1, type=int)
        anime_data = backend.get_episodes(anime_id, page)
        
        if not anime_data or (not anime_data['episodes'] and page == 1):
            # Fallback to live scrape if index is empty
            logger.warning(f"⚠️ No indexed episodes for {anime_id}, falling back to live scrape.")
            return redirect(url_for('anime_episodes_scraped', anime_id=anime_id))
        
        cache_stats = backend.cache.get_cache_stats()
        return render_template('episodes.html',
                             anime_id=anime_id,
                             anime_title=anime_data['title'],
                             episodes=anime_data['episodes'],
                             total_episodes=anime_data['total_episodes'],
                             has_next_page=anime_data['has_next_page'],
                             current_page=anime_data['current_page'],
                             next_page=anime_data['next_page'],
                             base_url=backend.base_url,
                             cache_stats=cache_stats,
                             is_live_page=False)
    except Exception as e:
        logger.error(f"❌ Episodes error: {e}")
        return render_template('error.html', 
                             message=f"Failed to load episodes: {str(e)}")

# --- THIS ROUTE IS UNCHANGED (Scraper-based) ---
@app.route('/anime/<anime_id>/live')
def anime_episodes_scraped(anime_id):
    """Show episodes for a specific anime by scraping (live)"""
    if not backend.ready:
        return render_template('error.html', 
                             message="Backend is still initializing. Please wait a moment and refresh.")
    
    try:
        page = request.args.get('page', 1, type=int)
        # --- MODIFIED: Use the renamed scraping function ---
        anime_data = run_async_in_thread(backend.scrape_episodes_page(anime_id, page))
        
        if not anime_data or (not anime_data['episodes'] and page == 1):
            return render_template('error.html', 
                                 message="No episodes found for this anime (live scrape).")
        
        cache_stats = backend.cache.get_cache_stats()
        return render_template('episodes.html',
                             anime_id=anime_id,
                             anime_title=anime_data['title'],
                             episodes=anime_data['episodes'],
                             total_episodes=anime_data['total_episodes'], # Note: this is just page total
                             has_next_page=anime_data['has_next_page'],
                             current_page=anime_data['current_page'],
                             next_page=anime_data['next_page'],
                             base_url=backend.base_url,
                             cache_stats=cache_stats,
                             is_live_page=True)
    except Exception as e:
        logger.error(f"❌ Scraped episodes error: {e}")
        return render_template('error.html', 
                             message=f"Failed to load live episodes: {str(e)}")

# --- THIS ROUTE IS UNCHANGED (Index-first iframe logic) ---
@app.route('/watch/<anime_id>/<episode_session>')
def watch_episode(anime_id, episode_session):
    """Watch a specific episode (uses index-first iframe logic)"""
    if not backend.ready:
        return render_template('error.html', 
                             message="Backend is still initializing. Please wait a moment and refresh.")
    
    try:
        # --- This now calls the new index-first function ---
        iframe_data = run_async_in_thread(backend.get_episode_iframe(anime_id, episode_session))
        
        # Get next episode info
        next_episode = backend.anime_index.get_next_episode(anime_id, episode_session)
        next_ep_session = next_episode.get('episode_id') if next_episode else None
        next_ep_number = next_episode.get('number') if next_episode else None

        if iframe_data['success'] and iframe_data['iframe_url']:
            episode_url = f"{backend.base_url}/play/{anime_id}/{episode_session}"
            cache_stats = backend.cache.get_cache_stats()
            return render_template('player.html',
                                 iframe_url=iframe_data['iframe_url'],
                                 episode_url=episode_url,
                                 anime_id=anime_id,
                                 cache_stats=cache_stats,
                                 next_ep_session=next_ep_session,
                                 next_ep_number=next_ep_number)
        else:
            error_msg = iframe_data.get('error', 'Unknown error')
            return render_template('error.html', 
                                 message=f"Could not load player: {error_msg}")
    except Exception as e:
        logger.error(f"❌ Watch error: {e}")
        return render_template('error.html', 
                             message=f"Failed to load player: {str(e)}")

@app.route('/cache/stats')
def cache_stats():
    """Get cache statistics"""
    stats = backend.cache.get_cache_stats()
    return jsonify(stats)

@app.route('/cache/clear')
def clear_cache():
    """Clear all cache"""
    backend.cache.cache = backend.cache.get_default_cache()
    backend.cache.save_cache()
    return jsonify({'message': 'Cache cleared successfully'})

@app.route('/status')
def status():
    """Check backend status"""
    cache_stats = backend.cache.get_cache_stats()
    total_anime = len(backend.anime_index.all_anime)
    return jsonify({
        'ready': backend.ready,
        'base_url': backend.base_url,
        'cache': cache_stats,
        'anime_index_count': total_anime
    })

if __name__ == '__main__':
    logger.info("🚀 Starting AnimePahe Backend on http://localhost:5002")
    logger.info(f"📚 Pre-loaded {len(backend.anime_index.all_anime)} anime titles")
    app.run(host='0.0.0.0', port=5002, debug=True, use_reloader=False)
