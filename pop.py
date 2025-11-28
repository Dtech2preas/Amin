#!/usr/bin/env python3
"""
Script to update popular anime from our JSON index and save to cache
Run this separately to update the popular anime data
"""

import json
import logging
import glob
import os
from datetime import datetime

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class PopularAnimeUpdater:
    def __init__(self):
        self.anime_dir = 'anime_index'
        self.cache_file = 'data.json'
        self.all_anime = []
        
    def load_all_anime(self):
        """Load all anime from JSON files"""
        try:
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
    
    def get_popular_anime_from_index(self):
        """Get popular anime by searching for specific titles in our index"""
        # List of popular anime we want to include
        popular_titles = [
            "Jujutsu Kaisen",
            "One Piece", 
            "Dan Da Dan",
            "Kaiju No. 8",
            "Kaiju no. 8",
            "Black Clover",
            "Demon Slayer",
            "Akame ga Kill",
            "Akame ga Kill!",
            "Chainsaw Man",
            "Naruto",
            "Bleach",
            "Eminence in Shadow",
            "Attack on Titan",
            "My Hero Academia",
            "Spy x Family",
            "Tokyo Revengers",
            "Dr. Stone",
            "Blue Lock",
            "Haikyuu",
            "One Punch Man",
            "Mob Psycho 100",
            "Hunter x Hunter",
            "Death Note",
            "Fullmetal Alchemist: Brotherhood",
            "Code Geass",
            "Steins;Gate",
            "Re:Zero",
            "Konosuba",
            "Overlord"
        ]
        
        found_anime = []
        seen_ids = set()
        
        logger.info(f"🔍 Searching for {len(popular_titles)} popular anime in index...")
        
        for title in popular_titles:
            # Search for exact matches first
            for anime in self.all_anime:
                anime_title = anime.get('title', '').lower()
                search_title = title.lower()
                
                # Check for exact match or close match
                if (search_title == anime_title or 
                    search_title in anime_title or 
                    anime_title.startswith(search_title)):
                    
                    anime_id = anime.get('id')
                    if anime_id and anime_id not in seen_ids:
                        seen_ids.add(anime_id)
                        found_anime.append({
                            'title': anime.get('title'),
                            'id': anime_id,
                            'url': f"https://animepahe.si/anime/{anime_id}"
                        })
                        logger.info(f"✅ Found: {anime.get('title')}")
                        break
        
        # If we didn't find enough, try fuzzy matching for the most important ones
        important_titles = ["Jujutsu Kaisen", "One Piece", "Dan Da Dan", "Kaiju No. 8", "Chainsaw Man"]
        
        for title in important_titles:
            if title not in [a['title'] for a in found_anime]:
                # Try to find similar titles
                for anime in self.all_anime:
                    anime_title = anime.get('title', '').lower()
                    if any(keyword in anime_title for keyword in title.lower().split()):
                        anime_id = anime.get('id')
                        if anime_id and anime_id not in seen_ids:
                            seen_ids.add(anime_id)
                            found_anime.append({
                                'title': anime.get('title'),
                                'id': anime_id,
                                'url': f"https://animepahe.si/anime/{anime_id}"
                            })
                            logger.info(f"✅ Found (fuzzy): {anime.get('title')}")
                            break
        
        return found_anime
    
    def get_default_popular_anime(self):
        """Get default popular anime list as fallback"""
        default_popular = [
            {
                "title": "One Piece",
                "id": "9b2f4c67-24e3-7a94-37b9-f2c1d1b5662a",
                "url": "https://animepahe.si/anime/9b2f4c67-24e3-7a94-37b9-f2c1d1b5662a"
            },
            {
                "title": "Naruto",
                "id": "7f7b1f1a-3b3a-1a2b-2c3d-4e5f6a7b8c9d",
                "url": "https://animepahe.si/anime/7f7b1f1a-3b3a-1a2b-2c3d-4e5f6a7b8c9d"
            },
            {
                "title": "Jujutsu Kaisen",
                "id": "c3d4e5f6-a7b8-9c0d-1e2f-3a4b5c6d7e8f",
                "url": "https://animepahe.si/anime/c3d4e5f6-a7b8-9c0d-1e2f-3a4b5c6d7e8f"
            },
            {
                "title": "Demon Slayer",
                "id": "f6a7b8c9-d0e1-2f3a-4b5c-6d7e8f9a0b1c",
                "url": "https://animepahe.si/anime/f6a7b8c9-d0e1-2f3a-4b5c-6d7e8f9a0b1c"
            },
            {
                "title": "Chainsaw Man",
                "id": "d4e5f6a7-b8c9-0d1e-2f3a-4b5c6d7e8f9a",
                "url": "https://animepahe.si/anime/d4e5f6a7-b8c9-0d1e-2f3a-4b5c6d7e8f9a"
            },
            {
                "title": "Attack on Titan",
                "id": "e5f6a7b8-c9d0-1e2f-3a4b-5c6d7e8f9a0b",
                "url": "https://animepahe.si/anime/e5f6a7b8-c9d0-1e2f-3a4b-5c6d7e8f9a0b"
            },
            {
                "title": "My Hero Academia",
                "id": "a7b8c9d0-e1f2-3a4b-5c6d-7e8f9a0b1c2d",
                "url": "https://animepahe.si/anime/a7b8c9d0-e1f2-3a4b-5c6d-7e8f9a0b1c2d"
            },
            {
                "title": "Spy x Family",
                "id": "b8c9d0e1-f2a3-4b5c-6d7e-8f9a0b1c2d3e",
                "url": "https://animepahe.si/anime/b8c9d0e1-f2a3-4b5c-6d7e-8f9a0b1c2d3e"
            },
            {
                "title": "Black Clover",
                "id": "a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d",
                "url": "https://animepahe.si/anime/a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d"
            },
            {
                "title": "Bleach",
                "id": "b2c3d4e5-f6a7-8b9c-0d1e-2f3a4b5c6d7e",
                "url": "https://animepahe.si/anime/b2c3d4e5-f6a7-8b9c-0d1e-2f3a4b5c6d7e"
            },
            {
                "title": "Eminence in Shadow",
                "id": "c9d0e1f2-a3b4-5c6d-7e8f-9a0b1c2d3e4f",
                "url": "https://animepahe.si/anime/c9d0e1f2-a3b4-5c6d-7e8f-9a0b1c2d3e4f"
            },
            {
                "title": "Dan Da Dan",
                "id": "d0e1f2a3-b4c5-6d7e-8f9a-0b1c2d3e4f5a",
                "url": "https://animepahe.si/anime/d0e1f2a3-b4c5-6d7e-8f9a-0b1c2d3e4f5a"
            }
        ]
        return default_popular
    
    def save_to_cache(self, anime_list):
        """Save popular anime to cache file"""
        try:
            # Load existing cache
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    cache = json.load(f)
            except FileNotFoundError:
                cache = {
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
            
            # Update popular anime
            cache['popular_anime'] = {
                'anime': anime_list,
                'timestamp': datetime.now().isoformat(),
                'count': len(anime_list)
            }
            
            # Update metadata
            cache['metadata']['last_updated'] = datetime.now().isoformat()
            
            # Save back to file
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache, f, indent=2, ensure_ascii=False)
            
            logger.info(f"💾 Saved {len(anime_list)} popular anime to cache file: {self.cache_file}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Error saving to cache: {e}")
            return False

def main():
    """Main function to update popular anime"""
    logger.info("🚀 Starting Popular Anime Updater")
    
    updater = PopularAnimeUpdater()
    
    try:
        # Load all anime from JSON files
        updater.load_all_anime()
        
        # Get popular anime from our index
        popular_anime = updater.get_popular_anime_from_index()
        
        # If not enough found, use default
        if len(popular_anime) < 8:
            logger.warning(f"❌ Only found {len(popular_anime)} popular anime, using default data")
            popular_anime = updater.get_default_popular_anime()
        
        # Save to cache
        success = updater.save_to_cache(popular_anime)
        
        if success:
            logger.info(f"✅ Successfully updated cache with {len(popular_anime)} popular anime")
            print(f"\n🔥 Updated {len(popular_anime)} popular anime:")
            for anime in popular_anime:
                print(f"   • {anime['title']}")
        else:
            logger.error("❌ Failed to save popular anime to cache")
            
    except Exception as e:
        logger.error(f"❌ Update failed: {e}")
        # Try to save default data
        popular_anime = updater.get_default_popular_anime()
        success = updater.save_to_cache(popular_anime)
        if success:
            logger.info(f"✅ Saved default data with {len(popular_anime)} popular anime")
        else:
            logger.error("❌ Failed to save default data")

if __name__ == '__main__':
    main()
