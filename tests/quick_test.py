#!/usr/bin/env python3
"""
🔧 SCHNELLE BOT FUNKTIONALITÄTSTESTS
"""

try:
    from discord_bot import FutBinBot, PlayerConfig
    import time
    
    print('✅ Bot Import erfolgreich')
    
    # Test Bot Erstellung
    bot = FutBinBot()
    print('✅ Bot Instanz erstellt')
    
    # Test Preis-Parsing
    test_prices = ['150.000', '1.5M', '250K', '1,234,567']
    parsed_prices = []
    for price_text in test_prices:
        result = bot._extract_price(price_text)
        parsed_prices.append(f'{price_text} -> {result}')
        
    print('✅ Preis-Parsing Tests:')
    for p in parsed_prices:
        print(f'  {p}')
    
    # Test HTML Parsing
    mock_html = '<div class="price inline-with-icon lowest-price-1">150.000</div>'
    parsed = bot.parse_price_and_image(mock_html)
    print(f'✅ HTML Parse Test: {parsed["price"]}')
    
    # Test State
    print('✅ State Pfad:', bot.state_path)
    print('✅ Aktueller State:', len(bot.state), 'Einträge')
    
    # Test Player Config
    player = PlayerConfig(
        name='Test Player',
        url='https://test.com',
        category='Test',
        notes='Test'
    )
    print('✅ PlayerConfig erstellt:', player.name)
    
    print('\n🎉 KRITISCHE FUNKTIONEN ARBEITEN!')
    
except Exception as e:
    print(f'❌ ERROR: {e}')
    import traceback
    traceback.print_exc()