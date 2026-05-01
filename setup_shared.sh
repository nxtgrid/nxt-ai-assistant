#!/bin/bash
# Quick setup script for shared module

set -e

ANANSI_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "🔧 Setting up Anansi shared module..."
echo "📍 Project root: $ANANSI_ROOT"
echo ""

# 1. Setup PYTHONPATH
echo "✅ Step 1: Setting up PYTHONPATH..."
export PYTHONPATH="$ANANSI_ROOT:$PYTHONPATH"
echo "   PYTHONPATH=$PYTHONPATH"
echo ""

# 2. Test shared module imports
echo "✅ Step 2: Testing shared module imports..."
python3 << EOF
import sys
sys.path.insert(0, "$ANANSI_ROOT")

try:
    from shared.utils import get_logger
    print("   ✅ shared.utils imported successfully")
except Exception as e:
    print(f"   ❌ shared.utils import failed: {e}")
    sys.exit(1)

try:
    from shared.auth import AuthService
    print("   ✅ shared.auth imported successfully")
except Exception as e:
    print(f"   ❌ shared.auth import failed: {e}")
    sys.exit(1)

try:
    from shared.config import db_settings
    print("   ✅ shared.config imported successfully")
except Exception as e:
    print(f"   ❌ shared.config import failed: {e}")
    sys.exit(1)

try:
    from shared.database import DatabaseManager
    print("   ✅ shared.database imported successfully")
except Exception as e:
    print(f"   ❌ shared.database import failed: {e}")
    sys.exit(1)

print("")
print("🎉 All shared module imports successful!")
EOF

echo ""
echo "✅ Step 3: Verifying directory structure..."
if [ -d "$ANANSI_ROOT/shared" ]; then
    echo "   ✅ shared/ directory exists"
    echo "   📁 Contents:"
    ls -l "$ANANSI_ROOT/shared" | grep "^d" | awk '{print "      - " $9}'
else
    echo "   ❌ shared/ directory not found!"
    exit 1
fi

echo ""
echo "✅ Step 4: Checking dependencies..."
python3 << EOF
import sys
sys.path.insert(0, "$ANANSI_ROOT")

required = ['loguru', 'pydantic', 'pydantic_settings']
missing = []

for pkg in required:
    try:
        __import__(pkg)
        print(f"   ✅ {pkg} installed")
    except ImportError:
        print(f"   ❌ {pkg} NOT installed")
        missing.append(pkg)

if missing:
    print("")
    print("⚠️  Missing dependencies. Install with:")
    print(f"   pip install {' '.join(missing)}")
    sys.exit(1)
EOF

echo ""
echo "=================================================="
echo "✅ Setup complete!"
echo "=================================================="
echo ""
echo "📝 Next steps:"
echo ""
echo "1. Add to your shell profile (~/.bashrc or ~/.zshrc):"
echo "   export PYTHONPATH=\"$ANANSI_ROOT:\$PYTHONPATH\""
echo ""
echo "2. Or source this script when needed:"
echo "   source $ANANSI_ROOT/setup_shared.sh"
echo ""
echo "3. Read the documentation:"
echo "   - shared/README.md - Module documentation"
echo "   - SHARED_CODE_MIGRATION.md - Migration guide"
echo "   - REFACTORING_SUMMARY.md - What was done"
echo ""
echo "4. Test in your projects:"
echo "   cd chat_orchestrator && python3 -c 'from shared.utils import get_logger; print(\"✅ Works!\")'"
echo ""
echo "=================================================="
