#!/usr/bin/env python3
"""
CodeFlow AI Reviewer - Main Flask Application
AI-powered GitHub PR code review bot with OpenAI GPT-4 integration
"""

import os
import json
import logging
import hmac
import hashlib
from datetime import datetime
from functools import wraps

from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
import openai
from github import Github, GithubException
from loguru import logger

# Load environment variables
load_dotenv()

# Initialize logging
logger.remove()  # Remove default handler
logger.add(
    lambda msg: print(msg, end=''),
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
    level="INFO"
)

# Initialize Flask app
app = Flask(__name__)
CORS(app)

# Configuration
app.config['JSON_SORT_KEYS'] = False
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GITHUB_WEBHOOK_SECRET = os.getenv('GITHUB_WEBHOOK_SECRET')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
FLASK_ENV = os.getenv('FLASK_ENV', 'development')

# Initialize OpenAI
openai.api_key = OPENAI_API_KEY
GPT_MODEL = os.getenv('OPENAI_MODEL', 'gpt-4')
GPT_MAX_TOKENS = int(os.getenv('OPENAI_MAX_TOKENS', '2000'))

# Initialize GitHub
gh = Github(GITHUB_TOKEN)


def verify_webhook_signature(request_body, signature):
    """
    Verify GitHub webhook signature for security
    """
    if not GITHUB_WEBHOOK_SECRET:
        return True
    
    expected_signature = 'sha256=' + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(),
        request_body,
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(signature, expected_signature)


def require_github_webhook(f):
    """
    Decorator to verify GitHub webhook authenticity
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        signature = request.headers.get('X-Hub-Signature-256', '')
        if not verify_webhook_signature(request.data, signature):
            logger.warning("Invalid webhook signature received")
            return jsonify({'error': 'Invalid signature'}), 401
        return f(*args, **kwargs)
    return decorated_function


def analyze_code_with_gpt(code, language, file_name):
    """
    Use OpenAI GPT-4 to analyze code and provide review suggestions
    """
    try:
        prompt = f"""
You are an expert code reviewer. Analyze the following {language} code and provide specific, actionable feedback.

File: {file_name}
Language: {language}

Code:
```{language}
{code}
```

Provide review feedback in this JSON format:
{{
    "summary": "Brief overview of the code",
    "issues": [
        {{
            "line": line_number_or_range,
            "severity": "critical|high|medium|low",
            "category": "security|performance|style|maintainability|bug",
            "message": "Specific issue description",
            "suggestion": "How to fix it"
        }}
    ],
    "positive_aspects": ["List of good practices"],
    "overall_score": 0-100
}}

Be concise but thorough. Focus on actionable items.
"""
        
        response = openai.ChatCompletion.create(
            model=GPT_MODEL,
            messages=[
                {"role": "system", "content": "You are an expert code reviewer that provides specific, actionable feedback."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=GPT_MAX_TOKENS,
            top_p=1.0
        )
        
        # Parse response
        try:
            review_data = json.loads(response['choices'][0]['message']['content'])
            return review_data
        except json.JSONDecodeError:
            # Return raw response if not JSON
            return {"raw_response": response['choices'][0]['message']['content']}
            
    except Exception as e:
        logger.error(f"GPT-4 analysis failed: {str(e)}")
        return {"error": str(e)}


def post_review_comment(repo_name, pr_number, review_data):
    """
    Post code review comments to GitHub PR
    """
    try:
        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        
        if not review_data or 'issues' not in review_data:
            return False
        
        # Create review comment
        comment_body = f"""🤖 **CodeFlow AI Review**

**Summary**: {review_data.get('summary', 'Code analysis complete')}

**Overall Score**: {review_data.get('overall_score', 'N/A')}/100

### Issues Found ({len(review_data.get('issues', []))})\n"""
        
        for issue in review_data.get('issues', []):
            severity_emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
            emoji = severity_emoji.get(issue.get('severity', 'low'), '🔵')
            
            comment_body += f"""
{emoji} **{issue.get('severity', 'unknown').upper()}** - {issue.get('category', 'general')}
- **Line**: {issue.get('line', 'N/A')}
- **Issue**: {issue.get('message', 'No description')}
- **Fix**: {issue.get('suggestion', 'See issue description')}
"""
        
        if review_data.get('positive_aspects'):
            comment_body += f"\n### ✅ Positive Aspects\n"
            for aspect in review_data.get('positive_aspects', []):
                comment_body += f"- {aspect}\n"
        
        comment_body += "\n---\n*Powered by CodeFlow AI Reviewer | [GitHub](https://github.com/SnakeEye-sudo/codeflow-ai-reviewer)*"
        
        # Post comment
        pr.create_issue_comment(comment_body)
        logger.info(f"Review comment posted to PR #{pr_number}")
        return True
        
    except GithubException as e:
        logger.error(f"GitHub API error: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"Failed to post review: {str(e)}")
        return False


@app.route('/', methods=['GET'])
def health_check():
    """
    Health check endpoint
    """
    return jsonify({
        'status': 'alive',
        'service': 'CodeFlow AI Reviewer',
        'version': '1.0.0',
        'timestamp': datetime.utcnow().isoformat()
    }), 200


@app.route('/webhook', methods=['POST'])
@require_github_webhook
def github_webhook():
    """
    GitHub webhook handler for PR events
    """
    payload = request.json
    action = payload.get('action')
    
    logger.info(f"Webhook received: action={action}")
    
    # Only process opened and synchronize (updated) events
    if action not in ['opened', 'synchronize']:
        return jsonify({'message': 'Event ignored'}), 200
    
    try:
        pr = payload.get('pull_request')
        repo = payload.get('repository')
        
        if not pr or not repo:
            return jsonify({'error': 'Invalid payload'}), 400
        
        pr_number = pr.get('number')
        repo_name = repo.get('full_name')
        changed_files = pr.get('changed_files', 0)
        
        logger.info(f"Processing PR #{pr_number} in {repo_name} ({changed_files} files changed)")
        
        # Get PR details
        gh_repo = gh.get_repo(repo_name)
        gh_pr = gh_repo.get_pull(pr_number)
        
        # Analyze changed files
        reviews_to_post = []
        for file in gh_pr.get_files():
            if file.changes > 0:  # Only analyze modified lines
                logger.info(f"Analyzing file: {file.filename}")
                
                # Determine language from file extension
                ext = file.filename.split('.')[-1]
                language_map = {
                    'py': 'python',
                    'js': 'javascript',
                    'ts': 'typescript',
                    'jsx': 'jsx',
                    'tsx': 'tsx',
                    'java': 'java',
                    'go': 'go',
                    'rs': 'rust',
                    'cpp': 'cpp',
                    'c': 'c',
                    'php': 'php',
                    'rb': 'ruby',
                    'sql': 'sql'
                }
                language = language_map.get(ext, 'text')
                
                # Get patch (only new/modified code)
                if file.patch:
                    review = analyze_code_with_gpt(file.patch, language, file.filename)
                    reviews_to_post.append({
                        'file': file.filename,
                        'review': review
                    })
        
        # Post reviews
        if reviews_to_post:
            for item in reviews_to_post:
                post_review_comment(repo_name, pr_number, item['review'])
        
        logger.info(f"Review complete for PR #{pr_number}")
        return jsonify({
            'success': True,
            'pr_number': pr_number,
            'files_analyzed': len(reviews_to_post),
            'message': 'Code review completed'
        }), 200
        
    except Exception as e:
        logger.error(f"Webhook processing error: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/review', methods=['POST'])
def manual_review():
    """
    Manual code review endpoint (for testing without webhooks)
    """
    try:
        data = request.json
        code = data.get('code', '')
        language = data.get('language', 'python')
        file_name = data.get('file_name', 'code.txt')
        
        if not code:
            return jsonify({'error': 'Code is required'}), 400
        
        logger.info(f"Manual review requested for {file_name}")
        
        review = analyze_code_with_gpt(code, language, file_name)
        
        return jsonify({
            'success': True,
            'file': file_name,
            'language': language,
            'review': review
        }), 200
        
    except Exception as e:
        logger.error(f"Manual review error: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/status', methods=['GET'])
def status():
    """
    Service status endpoint
    """
    return jsonify({
        'service': 'CodeFlow AI Reviewer',
        'status': 'operational',
        'github_connected': bool(GITHUB_TOKEN),
        'openai_connected': bool(OPENAI_API_KEY),
        'model': GPT_MODEL,
        'environment': FLASK_ENV,
        'timestamp': datetime.utcnow().isoformat()
    }), 200


@app.errorhandler(404)
def not_found(error):
    """
    Handle 404 errors
    """
    return jsonify({'error': 'Endpoint not found'}), 404


@app.errorhandler(500)
def server_error(error):
    """
    Handle 500 errors
    """
    logger.error(f"Server error: {str(error)}")
    return jsonify({'error': 'Internal server error'}), 500


if __name__ == '__main__':
    port = int(os.getenv('FLASK_PORT', 5000))
    debug = FLASK_ENV == 'development'
    
    logger.info(f"Starting CodeFlow AI Reviewer on port {port}")
    logger.info(f"Environment: {FLASK_ENV}")
    logger.info(f"Model: {GPT_MODEL}")
    
    app.run(host='0.0.0.0', port=port, debug=debug)
