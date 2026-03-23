#!/bin/bash
# Download all NBA team logos from ESPN CDN
# Run this script from the project root directory

mkdir -p nba_logos
cd nba_logos

echo "Downloading NBA team logos..."

# All 30 NBA teams
teams="atl bos bkn cha chi cle dal den det gsw hou ind lac lal mem mia mil min nop nyk okc orl phi phx por sac sas tor utah was"

for team in $teams; do
    echo "Downloading ${team}.png..."
    curl -s -o "${team}.png" \
        "https://a.espncdn.com/i/teamlogos/nba/500/scoreboard/${team}.png"
    
    if [ $? -eq 0 ]; then
        echo "  ✓ ${team}.png downloaded"
    else
        echo "  ✗ Failed to download ${team}.png"
    fi
done

echo ""
echo "✓ Logo download complete!"
echo "Total logos downloaded: $(ls -1 *.png 2>/dev/null | wc -l)"
