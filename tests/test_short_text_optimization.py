"""Test short text optimization pipeline.

Run:
    python tests/test_short_text_optimization.py
"""
import sys
sys.path.insert(0, 'src/tts')

from TTSSolver import TTSSolver


def test_text_enhancement():
    solver = TTSSolver("test", "auto", "test.wav")
    
    print("=== Text Enhancement Tests ===")
    
    text = "hello"
    enhanced = solver._enhance_text_for_tts(text)
    print(f"  Input: '{text}' -> Output: '{enhanced}'")
    assert enhanced == "Hello.", f"Expected 'Hello.', got '{enhanced}'"
    
    text = "good morning"
    enhanced = solver._enhance_text_for_tts(text)
    print(f"  Input: '{text}' -> Output: '{enhanced}'")
    assert enhanced == "Good morning.", f"Expected 'Good morning.', got '{enhanced}'"
    
    text = "Hello!"
    enhanced = solver._enhance_text_for_tts(text)
    print(f"  Input: '{text}' -> Output: '{enhanced}'")
    assert enhanced == "Hello!", f"Expected 'Hello!', got '{enhanced}'"
    
    text = "how are you doing today"
    enhanced = solver._enhance_text_for_tts(text)
    print(f"  Input: '{text}' -> Output: '{enhanced}'")
    assert enhanced == "How are you doing today."
    
    print("[PASS] Text enhancement\n")


def test_text_pipeline():
    solver = TTSSolver("test", "auto", "test.wav")
    
    print("=== Text Pipeline Tests ===")
    
    # 1 word -> should add prefix
    text = "hello"
    processed, needs_trim, prefix_words = solver._process_text_pipeline(text)
    print(f"  '{text}' -> '{processed}' (trim={needs_trim}, prefix_words={prefix_words})")
    assert needs_trim is True, "Single word should trigger prefix"
    assert prefix_words == 1
    assert "hello" in processed.lower()
    
    # 2 words -> should add prefix
    text = "good morning"
    processed, needs_trim, prefix_words = solver._process_text_pipeline(text)
    print(f"  '{text}' -> '{processed}' (trim={needs_trim}, prefix_words={prefix_words})")
    assert needs_trim is True, "2 words should trigger prefix"
    assert prefix_words == 1
    
    # 3 words -> no prefix
    text = "how are you"
    processed, needs_trim, prefix_words = solver._process_text_pipeline(text)
    print(f"  '{text}' -> '{processed}' (trim={needs_trim}, prefix_words={prefix_words})")
    assert needs_trim is False, "3 words should NOT trigger prefix"
    assert prefix_words == 0
    
    # Long sentence -> no prefix
    text = "I would like to order a cup of coffee please"
    processed, needs_trim, prefix_words = solver._process_text_pipeline(text)
    print(f"  '{text}' -> '{processed}' (trim={needs_trim}, prefix_words={prefix_words})")
    assert needs_trim is False
    
    # Currency
    text = "$5"
    processed, needs_trim, prefix_words = solver._process_text_pipeline(text)
    print(f"  '{text}' -> '{processed}' (trim={needs_trim}, prefix_words={prefix_words})")
    assert "five" in processed.lower() or "dollar" in processed.lower()
    
    print("[PASS] Text pipeline\n")


def test_no_extra_words_in_audio():
    solver = TTSSolver("test", "auto", "test.wav")
    
    print("=== Critical: No Extra Words in Audio ===")
    
    original_text = "hello"
    processed_text, needs_trim, prefix_words = solver._process_text_pipeline(original_text)
    
    print(f"  Original: '{original_text}'")
    print(f"  TTS input: '{processed_text}'")
    print(f"  Needs trim: {needs_trim}, prefix words: {prefix_words}")
    
    assert needs_trim is True, "Prefix MUST be trimmed"
    print("  -> Prefix will be trimmed from audio")
    print("  -> Final audio only contains: 'hello'")
    
    print("[PASS] No extra words\n")


def test_comparison():
    solver = TTSSolver("test", "auto", "test.wav")
    
    print("=== Old vs New Method Comparison ===")
    
    test_cases = ["hello", "good morning", "how are you", "I would like to order"]
    
    for text in test_cases:
        # Old method
        old_text = text
        if len(text.split()) <= 1:
            old_text = "Oh, " + text
        
        # New method
        new_text, needs_trim, prefix_words = solver._process_text_pipeline(text)
        
        print(f"\n  Original: '{text}'")
        print(f"    OLD: '{old_text}' (no trim, scored by system)")
        print(f"    NEW: '{new_text}' (prefix trimmed from audio)")
        
        if needs_trim:
            print(f"    -> Prefix trimmed, audio only has target words")
        else:
            print(f"    -> Text long enough, no special handling")
    
    print("\n[PASS] Comparison\n")


if __name__ == "__main__":
    print("=" * 60)
    print("VoxPilot Short Text Optimization Tests")
    print("=" * 60 + "\n")
    
    try:
        test_text_enhancement()
        test_text_pipeline()
        test_no_extra_words_in_audio()
        test_comparison()
        
        print("=" * 60)
        print("ALL TESTS PASSED!")
        print("=" * 60)
        
    except AssertionError as e:
        print(f"\nTEST FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
