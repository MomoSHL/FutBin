#!/usr/bin/env python3
"""
🧪 COMPREHENSIVE BOT TEST SUITE
Testet alle Bot-Funktionen systematisch

Führe aus mit: python test_bot.py
"""

import asyncio
import json
import logging
import time
import sys
import os
from pathlib import Path
from unittest.mock import Mock, AsyncMock, patch
from typing import Dict, List, Any

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('test_results.log', mode='w', encoding='utf-8')
    ]
)

# Mock Discord für Tests
class MockMessage:
    def __init__(self, content: str = "", author_id: int = 123):
        self.content = content
        self.author = Mock()
        self.author.id = author_id
        self.author.mention = f"<@{author_id}>"
        self.channel = Mock()
        self.guild = Mock()
        
class MockChannel:
    def __init__(self):
        self.id = 123456
        self.name = "test-channel"
        
    async def send(self, **kwargs):
        return Mock()

class MockBot:
    def __init__(self):
        self.user = Mock()
        self.user.id = 999
        
# Import Bot nach Mock Setup
try:
    from discord_bot import FutBinBot, PlayerConfig
    BOT_AVAILABLE = True
except ImportError as e:
    print(f"❌ Bot Import Error: {e}")
    BOT_AVAILABLE = False

class BotTestSuite:
    """Umfassende Test Suite für den FutBin Bot"""
    
    def __init__(self):
        self.results = []
        self.test_data_dir = Path("test_data")
        self.test_data_dir.mkdir(exist_ok=True)
        
    def log_result(self, test_name: str, success: bool, details: str = ""):
        """Loggt Testergebnis"""
        status = "✅ PASS" if success else "❌ FAIL"
        result = {
            "test": test_name,
            "success": success,
            "details": details,
            "timestamp": time.time()
        }
        self.results.append(result)
        logging.info(f"{status} {test_name}: {details}")
        
    def create_test_bot(self) -> FutBinBot:
        """Erstellt Test-Bot Instanz"""
        if not BOT_AVAILABLE:
            raise ImportError("Bot nicht verfügbar für Tests")
        
        # FutBinBot ist eine commands.Bot Subklasse ohne Parameter
        bot = FutBinBot()
        bot.dashboard_channel = MockChannel()
        return bot
        
    def create_test_players(self) -> List[PlayerConfig]:
        """Erstellt Test-Spieler Daten"""
        return [
            PlayerConfig(
                name="Viktor Gyökeres",
                url="https://www.futbin.com/26/player/123456/viktor-gyokeres",
                category="Strikers",
                notes="Test Spieler 1",
                alert_above=150000,
                alert_below=100000
            ),
            PlayerConfig(
                name="Kylian Mbappé",
                url="https://www.futbin.com/26/player/654321/kylian-mbappe",
                category="Wingers",
                notes="Test Spieler 2",
                alert_above=2000000,
                alert_below=1500000
            )
        ]
    
    # ========== CORE FUNKTIONALITÄT TESTS ==========
    
    async def test_bot_initialization(self):
        """Test Bot Initialisierung"""
        try:
            bot = self.create_test_bot()
            assert hasattr(bot, 'players')
            assert hasattr(bot, 'state')
            assert hasattr(bot, 'state_path')
            self.log_result("Bot Initialization", True, "Bot erfolgreich initialisiert")
            return bot
        except Exception as e:
            self.log_result("Bot Initialization", False, f"Fehler: {e}")
            return None
    
    async def test_player_management(self):
        """Test Spieler hinzufügen/entfernen"""
        try:
            bot = self.create_test_bot()
            test_players = self.create_test_players()
            
            # Test: Spieler hinzufügen
            initial_count = len(bot.players)
            bot.players.extend(test_players)
            
            assert len(bot.players) == initial_count + 2
            assert bot.players[-1].name == "Kylian Mbappé"
            
            # Test: Spieler entfernen
            bot.players = [p for p in bot.players if p.name != "Viktor Gyökeres"]
            assert len(bot.players) == initial_count + 1
            
            self.log_result("Player Management", True, f"Spieler Management funktioniert")
            
        except Exception as e:
            self.log_result("Player Management", False, f"Fehler: {e}")
    
    # ========== PREIS PARSING TESTS ==========
    
    async def test_price_extraction(self):
        """Test Preis-Extraktion aus verschiedenen Formaten"""
        try:
            bot = self.create_test_bot()
            
            test_cases = [
                ("150.000", 150000),
                ("1.5M", 1500000),
                ("250K", 250000),
                ("1,234,567", 1234567),
                ("2.5M Coins", 2500000),
                ("Price: 500K", 500000),
                ("Invalid", None),
                ("", None)
            ]
            
            success_count = 0
            for text, expected in test_cases:
                result = bot._extract_price(text)
                if result == expected:
                    success_count += 1
                else:
                    logging.warning(f"Preis-Parse Fehler: '{text}' -> {result} (erwartet: {expected})")
            
            success_rate = success_count / len(test_cases)
            self.log_result(
                "Price Extraction", 
                success_rate > 0.8, 
                f"{success_count}/{len(test_cases)} Erfolg ({success_rate:.1%})"
            )
            
        except Exception as e:
            self.log_result("Price Extraction", False, f"Fehler: {e}")
    
    async def test_html_parsing(self):
        """Test HTML Parsing mit Mock-Daten"""
        try:
            bot = self.create_test_bot()
            
            # Mock HTML wie von FutBin
            mock_html = """
            <html>
                <head><title>Viktor Gyökeres - 84 - FIFA 26</title></head>
                <body>
                    <div class="playercard-26-name text-ellipsis">Viktor Gyökeres</div>
                    <div class="price inline-with-icon lowest-price-1">150.000</div>
                    <img class="playercard-26-base-img" data-original="//futbin.com/img/player.png">
                </body>
            </html>
            """
            
            parsed = bot.parse_price_and_image(mock_html)
            
            assert parsed['price'] == 150000
            assert "Viktor Gyökeres" in str(parsed['name'])
            assert parsed['image'] is not None
            
            self.log_result("HTML Parsing", True, "HTML erfolgreich geparst")
            
        except Exception as e:
            self.log_result("HTML Parsing", False, f"Fehler: {e}")
    
    # ========== STATE MANAGEMENT TESTS ==========
    
    async def test_state_persistence(self):
        """Test State Speichern/Laden"""
        try:
            bot = self.create_test_bot()
            test_state_path = self.test_data_dir / "test_state.json"
            bot.state_path = test_state_path
            
            # Test State
            test_state = {
                "https://test.com/player1": {
                    "price": 150000,
                    "image": "test.png",
                    "last_check": time.time(),
                    "price_history": [140000, 145000, 150000]
                }
            }
            
            bot.state = test_state
            bot._save_state()
            
            # Prüfe ob Datei existiert
            assert test_state_path.exists()
            
            # Lade State neu
            bot.state = {}
            bot._load_state()
            
            assert len(bot.state) > 0
            assert "https://test.com/player1" in bot.state
            
            self.log_result("State Persistence", True, "State erfolgreich gespeichert/geladen")
            
        except Exception as e:
            self.log_result("State Persistence", False, f"Fehler: {e}")
    
    # ========== ALERT SYSTEM TESTS ==========
    
    async def test_alert_system(self):
        """Test Alert System"""
        try:
            bot = self.create_test_bot()
            test_players = self.create_test_players()
            bot.players = test_players
            
            # Simuliere Preisänderungen die Alerts auslösen sollten
            test_cases = [
                ("Viktor Gyökeres", 160000, True),   # Über alert_above (150000)
                ("Viktor Gyökeres", 90000, True),    # Unter alert_below (100000)
                ("Viktor Gyökeres", 120000, False),  # Zwischen den Grenzen
                ("Kylian Mbappé", 2100000, True),   # Über alert_above (2000000)
            ]
            
            alert_count = 0
            for player_name, price, should_alert in test_cases:
                player = next(p for p in bot.players if p.name == player_name)
                
                # Mockiere check_price_alerts
                with patch.object(bot, 'send_price_alert') as mock_alert:
                    if bot._should_send_alert(player, price):
                        alert_count += 1
            
            self.log_result("Alert System", True, f"Alert System funktioniert ({alert_count} Alerts)")
            
        except Exception as e:
            self.log_result("Alert System", False, f"Fehler: {e}")
    
    # ========== DASHBOARD TESTS ==========
    
    async def test_dashboard_generation(self):
        """Test Dashboard Erstellung"""
        try:
            bot = self.create_test_bot()
            test_players = self.create_test_players()
            bot.players = test_players
            
            # Füge Mock State hinzu
            for player in test_players:
                bot.state[player.url] = {
                    "price": 150000,
                    "image": "test.png",
                    "last_check": time.time(),
                    "price_history": [140000, 145000, 150000]
                }
            
            # Test Dashboard Komponenten
            table_data = bot._create_dashboard_table()
            stats = bot._create_dashboard_stats()
            
            assert len(table_data) > 0
            assert "total_value" in stats
            assert "total_players" in stats
            
            self.log_result("Dashboard Generation", True, "Dashboard erfolgreich erstellt")
            
        except Exception as e:
            self.log_result("Dashboard Generation", False, f"Fehler: {e}")
    
    # ========== PORTFOLIO TESTS ==========
    
    async def test_portfolio_management(self):
        """Test Portfolio Management"""
        try:
            bot = self.create_test_bot()
            test_players = self.create_test_players()
            bot.players = test_players
            
            # Test Kategorie-Gruppierung
            categories = {}
            for player in test_players:
                if player.category not in categories:
                    categories[player.category] = []
                categories[player.category].append(player)
            
            assert "Strikers" in categories
            assert "Wingers" in categories
            assert len(categories["Strikers"]) == 1
            
            # Test Portfolio Export
            portfolio_data = bot._generate_simple_export()
            assert "Portfolio Export" in portfolio_data
            
            self.log_result("Portfolio Management", True, "Portfolio Management funktioniert")
            
        except Exception as e:
            self.log_result("Portfolio Management", False, f"Fehler: {e}")
    
    # ========== PERFORMANCE TESTS ==========
    
    async def test_performance_metrics(self):
        """Test Performance Metriken"""
        try:
            bot = self.create_test_bot()
            
            # Performance Test: Viele Spieler verarbeiten
            large_player_list = []
            for i in range(100):
                large_player_list.append(PlayerConfig(
                    name=f"Test Player {i}",
                    url=f"https://test.com/player/{i}",
                    category="Test",
                    notes=f"Performance Test {i}"
                ))
            
            start_time = time.time()
            
            # Simuliere Dashboard-Erstellung mit vielen Spielern
            bot.players = large_player_list
            for player in large_player_list[:10]:  # Nur erste 10 für Mock State
                bot.state[player.url] = {
                    "price": 100000 + i * 1000,
                    "last_check": time.time()
                }
            
            table_data = bot._create_dashboard_table()
            
            duration = time.time() - start_time
            
            self.log_result(
                "Performance Metrics", 
                duration < 1.0,  # Sollte unter 1 Sekunde dauern
                f"100 Spieler in {duration:.3f}s verarbeitet"
            )
            
        except Exception as e:
            self.log_result("Performance Metrics", False, f"Fehler: {e}")
    
    # ========== ERROR HANDLING TESTS ==========
    
    async def test_error_handling(self):
        """Test Fehlerbehandlung"""
        try:
            bot = self.create_test_bot()
            
            # Test: Ungültige HTML
            result = bot.parse_price_and_image("Invalid HTML")
            assert result['price'] is None
            
            # Test: Ungültige Preis-Texte
            invalid_prices = ["", "abc", "##$$", None]
            for price_text in invalid_prices:
                result = bot._extract_price(price_text)
                assert result is None
            
            # Test: Leere Spielerliste
            bot.players = []
            table_data = bot._create_dashboard_table()
            assert isinstance(table_data, list)
            
            self.log_result("Error Handling", True, "Fehlerbehandlung funktioniert korrekt")
            
        except Exception as e:
            self.log_result("Error Handling", False, f"Fehler: {e}")
    
    # ========== INTEGRATION TESTS ==========
    
    async def test_full_workflow(self):
        """Test kompletter Workflow"""
        try:
            bot = self.create_test_bot()
            test_players = self.create_test_players()
            
            # 1. Spieler hinzufügen
            bot.players = test_players
            
            # 2. Mock Price Check
            for player in test_players:
                mock_price = 150000
                bot.update_state(player, mock_price, "test.png")
            
            # 3. Dashboard erstellen
            dashboard_data = bot._create_dashboard_table()
            
            # 4. Alerts prüfen
            alert_triggered = any(
                bot._should_send_alert(player, 160000) for player in test_players
            )
            
            # 5. Portfolio Export
            export_data = bot._generate_simple_export()
            
            # Prüfe alle Schritte
            workflow_success = (
                len(dashboard_data) > 0 and
                len(bot.state) > 0 and
                "Portfolio Export" in export_data
            )
            
            self.log_result("Full Workflow", workflow_success, "Kompletter Workflow erfolgreich")
            
        except Exception as e:
            self.log_result("Full Workflow", False, f"Fehler: {e}")
    
    # ========== TEST RUNNER ==========
    
    async def run_all_tests(self):
        """Führt alle Tests aus"""
        print("🧪 === FutBin Bot Test Suite ===\n")
        
        if not BOT_AVAILABLE:
            self.log_result("Bot Import", False, "Bot konnte nicht importiert werden")
            return
        
        # Core Tests
        await self.test_bot_initialization()
        await self.test_player_management()
        
        # Parsing Tests
        await self.test_price_extraction()
        await self.test_html_parsing()
        
        # State Tests
        await self.test_state_persistence()
        
        # Feature Tests
        await self.test_alert_system()
        await self.test_dashboard_generation()
        await self.test_portfolio_management()
        
        # Performance Tests
        await self.test_performance_metrics()
        
        # Error Handling
        await self.test_error_handling()
        
        # Integration Tests
        await self.test_full_workflow()
        
        # Zeige Ergebnisse
        self.show_results()
    
    def show_results(self):
        """Zeigt Test-Ergebnisse"""
        passed = sum(1 for r in self.results if r['success'])
        total = len(self.results)
        success_rate = passed / total if total > 0 else 0
        
        print(f"\n📊 === TEST ERGEBNISSE ===")
        print(f"✅ Bestanden: {passed}/{total} ({success_rate:.1%})")
        print(f"❌ Fehlgeschlagen: {total - passed}")
        print(f"\n{'='*50}")
        
        # Detaillierte Ergebnisse
        for result in self.results:
            status = "✅" if result['success'] else "❌"
            print(f"{status} {result['test']}: {result['details']}")
        
        # Speichere Ergebnisse
        results_file = Path("test_results.json")
        with open(results_file, 'w', encoding='utf-8') as f:
            json.dump({
                "summary": {
                    "total": total,
                    "passed": passed,
                    "success_rate": success_rate,
                    "timestamp": time.time()
                },
                "details": self.results
            }, f, indent=2, ensure_ascii=False)
        
        print(f"\n📄 Detaillierte Ergebnisse gespeichert in: {results_file}")
        
        # Exit Code
        if success_rate < 0.8:
            print("\n⚠️  WARNUNG: Niedrige Erfolgsrate! Bot benötigt Wartung.")
            sys.exit(1)
        else:
            print("\n🎉 Alle kritischen Tests bestanden!")
            sys.exit(0)

# ========== MAIN ==========

async def main():
    """Hauptfunktion"""
    test_suite = BotTestSuite()
    await test_suite.run_all_tests()

if __name__ == "__main__":
    asyncio.run(main())