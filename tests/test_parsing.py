#!/usr/bin/env python3
"""
🔧 TEST PREIS-PARSING FIXES
"""

from discord_bot import FutBinBot
import time

print('🔧 TESTE PREIS-PARSING FIXES:')
bot = FutBinBot()

# Test deutsche Zahlenformate
test_cases = [
    '267.000',      # -> sollte 267000 werden
    '1.500.000',    # -> sollte 1500000 werden  
    '450K',         # -> sollte 450000 werden
    '1.5M',         # -> sollte 1500000 werden
    '189',          # -> sollte 189 werden
    '67',           # -> sollte 67 werden
    '53',           # -> sollte 53 werden
]

for test in test_cases:
    result = bot._extract_price(test)
    if result:
        print(f'  {test:>10} -> {result:>8,} Coins')
    else:
        print(f'  {test:>10} -> None')

print('\n✅ Preis-Parsing Tests abgeschlossen!')
print('🎯 Die Fixes sollten nun korrekte Tausender-Werte liefern!')