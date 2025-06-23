import asyncio, requests
from django.http import JsonResponse, Http404
from django.views.decorators.http import require_http_methods
from pathlib import Path
from django.conf import settings
from .utils import capture_sections_for_all_links, fetch_policies_parallel
from .models import Certification, Clause, Policy, Control
from django.db import transaction
import json
from playwright.sync_api import sync_playwright
from playwright.async_api import async_playwright
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.views.decorators.csrf import csrf_exempt



def get_certifications(request):
    if request.method != "GET":
        return JsonResponse({"status": "error", "message": "Only GET method is allowed."}, status=405)

    try:
        results = asyncio.run(capture_sections_for_all_links())
        return JsonResponse({
            "status": "success",
            "message": f"Captured {len(results)} section JSONs.",
            "files": [res["url"] for res in results]
        })
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=500)


@require_http_methods(["GET"])
def populate_database(request):
    """Endpoint to populate DB from JSON files with M2M relationships"""
    BASE_DIR = Path(settings.BASE_DIR)
    OUTPUT_DIR = BASE_DIR / "sections_output"
    
    stats = {
        'files_processed': 0,
        'certifications': 0,
        'clauses': 0,
        'policies': 0,
        'controls': 0,
        'policy_clause_links': 0,
        'control_clause_links': 0,
        'errors': []
    }

    if not OUTPUT_DIR.exists():
        return JsonResponse({
            "status": "error",
            "message": f"Directory not found: {OUTPUT_DIR}"
        }, status=404)

    try:
        with transaction.atomic():
            for json_file in OUTPUT_DIR.glob("*.json"):
                try:
                    stats['files_processed'] += 1
                    with open(json_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        
                        # Process certification
                        cert_name = ' '.join([part.upper() for part in json_file.stem.split('_')])
                        cert, created = Certification.objects.get_or_create(name=cert_name)
                        if created:
                            stats['certifications'] += 1
                        
                        # Process clauses
                        for section in data:
                            clause, created = Clause.objects.get_or_create(
                                certification=cert,
                                reference_id=section.get("referenceId", ""),
                                defaults={
                                    'display_identifier': section.get("displayIdentifier", ""),
                                    'title': section.get("title", ""),
                                    'description': section.get("description", ""),
                                    'original_id': section.get("id", "")
                                }
                            )
                            if created:
                                stats['clauses'] += 1
                            
                            # Process policies with M2M
                            for policy_data in section.get("programPolicyMapping", []):
                                policy, created = Policy.objects.get_or_create(
                                    policy_id=policy_data.get("shortName", ""),
                                    defaults={
                                        'policy_reference': policy_data.get("id", ""),
                                        'policy_doc': policy_data.get("description", ""),
                                        'title': policy_data.get("title", "")
                                    }
                                )
                                if created:
                                    stats['policies'] += 1
                                
                                # Add M2M relationship if not exists
                                if not clause.policies.filter(pk=policy.pk).exists():
                                    clause.policies.add(policy)
                                    stats['policy_clause_links'] += 1
                            
                            # Process controls with M2M
                            for subsection in section.get("subsections", []):
                                for control_data in subsection.get("programControlMapping", []):
                                    control, created = Control.objects.get_or_create(
                                        short_name=control_data.get("shortName", ""),
                                        defaults={
                                            'custom_short_name': control_data.get("customShortName", None),
                                            'name': control_data.get("name", ""),
                                            'description': control_data.get("description", ""),
                                            'original_id': control_data.get("id", "")
                                        }
                                    )
                                    if created:
                                        stats['controls'] += 1
                                    
                                    # Add M2M relationship if not exists
                                    if not clause.controls.filter(pk=control.pk).exists():
                                        clause.controls.add(control)
                                        stats['control_clause_links'] += 1
                
                except Exception as e:
                    stats['errors'].append({
                        'file': json_file.name,
                        'error': str(e)
                    })
        
        response_status = "success" if not stats['errors'] else "partial"
        status_code = 200 if not stats['errors'] else 207
        
        return JsonResponse({
            "status": response_status,
            "message": "Database population completed",
            "stats": stats
        }, status=status_code)
    
    except Exception as e:
        return JsonResponse({
            "status": "error",
            "message": f"Transaction failed: {str(e)}",
            "stats": stats
        }, status=500)

@require_http_methods(["GET"])
def get_population_status(request):
    """Check current database status with M2M counts"""
    from django.db.models import Count
    
    certifications = Certification.objects.count()
    clauses = Clause.objects.count()
    policies = Policy.objects.count()
    controls = Control.objects.count()
    
    # Get sample M2M relationship counts
    policy_with_clauses = Policy.objects.annotate(
        clause_count=Count('clauses')
    ).order_by('-clause_count').first()
    
    control_with_clauses = Control.objects.annotate(
        clause_count=Count('clauses')
    ).order_by('-clause_count').first()
    
    return JsonResponse({
        "status": "success",
        "data": {
            "certifications": certifications,
            "clauses": clauses,
            "policies": policies,
            "controls": controls,
            "sample_relationships": {
                "most_linked_policy": {
                    "id": policy_with_clauses.policy_id if policy_with_clauses else None,
                    "clause_count": policy_with_clauses.clause_count if policy_with_clauses else 0
                },
                "most_linked_control": {
                    "id": control_with_clauses.short_name if control_with_clauses else None,
                    "clause_count": control_with_clauses.clause_count if control_with_clauses else 0
                }
            }
        }
    })

OUTPUT_DIR = Path("policies_output")
OUTPUT_DIR.mkdir(exist_ok=True)


async def capture_policies_data():
    found = False
    collected_data = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        async def handle_response(response):
            nonlocal found, collected_data
            try:
                if "policies" in response.url and response.request.resource_type == "xhr":
                    found = True
                    print(f"✅ Found 'policies' API: {response.url}")
                    json_data = await response.json()

                    # Save JSON to file
                    output_path = OUTPUT_DIR / "trustcloud_policies.json"
                    with open(output_path, "w", encoding="utf-8") as f:
                        json.dump(json_data, f, ensure_ascii=False, indent=2)

                    control_ids_mapping_with_policies = []
                    for i in json_data:
                        policy_id = i.get("id")
                        control_ids = i.get("relatedControlIds")
                        security_group = i.get("securityGroup")
                        control_ids_mapping_with_policies.append({
                            "policy": policy_id,
                            "control_ids": control_ids,
                            "security_group":security_group
                        })

                    collected_data = control_ids_mapping_with_policies  # ✅ Set nonlocal
                    print(f"📁 Saved JSON to {output_path}")
            except Exception as e:
                print(f"⚠️ Error: {e}")

        page.on("response", handle_response)

        try:
            await page.goto("https://trust.trustcloud.ai/policies", timeout=30000)
            await page.wait_for_timeout(8000)  # Wait for XHR to complete
        except Exception as e:
            print(f"❌ Failed to load page: {e}")
        finally:
            await browser.close()

        if not found:
            print("❌ 'policies' API not found.")

    return collected_data


@csrf_exempt
def map_controls_with_policy(request):
    try:
        data = asyncio.run(capture_policies_data())
        if not data:
            return JsonResponse({"error": "No 'policies' API response captured"}, status=404)

        result = {
            "linked": [],
            "unmatched_policies": [],
            "unmatched_controls": []
        }

        for item in data:
            policy_ref = item.get("policy")
            control_ids = item.get("control_ids", [])

            try:
                policy = Policy.objects.get(policy_reference=policy_ref)
            except Policy.DoesNotExist:
                result["unmatched_policies"].append(policy_ref)
                continue

            matched_controls = []
            missing_controls = []

            for cid in control_ids:
                try:
                    ctrl = Control.objects.get(original_id=cid)
                    matched_controls.append(ctrl)
                except Control.DoesNotExist:
                    missing_controls.append(cid)

            # Update M2M relationship
            if matched_controls:
                policy.controls.set(matched_controls)
                policy.save()
                result["linked"].append({
                    "policy": policy_ref,
                    "matched_control_ids": [c.original_id for c in matched_controls]
                })

            if missing_controls:
                result["unmatched_controls"].append({
                    "policy": policy_ref,
                    "missing_control_ids": missing_controls
                })

        for i in data:
            policy = i.get("policy")
            security_group = i.get("security_group")
            if Policy.objects.filter(policy_reference=policy).exists():
                policyObj = Policy.objects.get(policy_reference=policy)
                policyObj.security_group = security_group
                policyObj.save()

        return JsonResponse(result, safe=False)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
    


from django.shortcuts import render, get_object_or_404
def certifications_view(request):
    certifications = Certification.objects.all().prefetch_related('clauses')
    return render(request, 'certifications.html', {
        'certifications': certifications
    })

def clause_detail_view(request, clause_id):
    clause = get_object_or_404(Clause, id=clause_id)
    return render(request, 'clause_detail.html', {
        'clause': clause
    })

def policies_view(request):
    policies = Policy.objects.all().prefetch_related('clauses', 'controls')
    
    # Get unique security groups
    security_groups = sorted(set(
        policy.security_group for policy in policies 
        if policy.security_group
    ))
    
    # Group policies by security group and title
    grouped_policies = {}
    for policy in policies:
        group = policy.security_group or 'None'
        title = policy.title or 'No Title'
        
        if group not in grouped_policies:
            grouped_policies[group] = {}
        if title not in grouped_policies[group]:
            grouped_policies[group][title] = []
        grouped_policies[group][title].append(policy)
    
    return render(request, 'policies.html', {
        'security_groups': security_groups,
        'grouped_policies': grouped_policies
    })

def clause_detail_api(request, clause_id):
    try:
        clause = Clause.objects.select_related('certification').get(id=clause_id)
    except Clause.DoesNotExist:
        raise Http404

    return JsonResponse({
        "id": clause.id,
        "reference_id": clause.reference_id,
        "display_identifier": clause.display_identifier,
        "title": clause.title,
        "description": clause.description,
        "certification": clause.certification.name,
    })


def control_detail_api(request, control_id):
    try:
        control = Control.objects.get(id=control_id)
    except Control.DoesNotExist:
        raise Http404

    return JsonResponse({
        "id": control.id,
        "short_name": control.short_name,
        "name": control.name,
        "description": control.description,
        "original_id": control.original_id,
    })
    

def policy_detail_api(request, policy_id):
    try:
        policy = Policy.objects.select_related().prefetch_related('clauses', 'controls').get(pk=policy_id)
    except Policy.DoesNotExist:
        raise Http404("Policy not found")

    data = {
        "Policy ID": policy.policy_id,
        "Title": policy.title,
        "Reference": policy.policy_reference,
        "Version": policy.policy_version,
        "Description": policy.policy_doc,
        "Security Group": policy.security_group,
        "Clauses": [clause.display_identifier for clause in policy.clauses.all()],
        "Controls": [control.short_name for control in policy.controls.all()],
    }
    return JsonResponse(data)

def control_detail(request, id):
    control = get_object_or_404(Control, id=id)
    return render(request, 'control_detail.html', {'control': control})


@csrf_exempt
def pulling_policies_from_eramba(request):
    """API endpoint to fetch policies from eramba"""
    policies = fetch_policies_parallel()
    return JsonResponse({
        "message": f"Successfully pulled {len(policies)} policies",
        "policies": policies,
        "policies_count": len(policies)
    }, status=200)

@csrf_exempt
def pulling_eramba_frameworkds(request):
    url = "https://www.eramba.org/api/proxy?endpoint=compliance-package-regulators"
    new_certifications = []
    updated_certifications = []

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
    except requests.exceptions.Timeout:
        return JsonResponse({"error": "Request to Eramba timed out."}, status=504)
    except requests.exceptions.RequestException as e:
        return JsonResponse({"error": f"Request failed: {str(e)}"}, status=500)

    try:
        content = response.json()
        data = content.get("data", [])
    except (ValueError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid JSON response from Eramba."}, status=502)

    for item in data:
        name = item.get("name")
        description = item.get("description")
        created_at = item.get("created")
        version = item.get("version")
        frameworkUrl = item.get("url")
        regulation_name = item.get("regulation_name")

        try:
            existing = Certification.objects.filter(name=name).first()
            if not existing:
                Certification.objects.create(
                    name=name,
                    slug=name.lower().replace(" ", "-"),
                    description=description,
                    url=frameworkUrl,
                    version=version,
                    regulation_name=regulation_name,
                    created_at=created_at,
                    updated_at=None
                )
                new_certifications.append(name)
            else:
                existing.version = version
                existing.description = description
                existing.url = frameworkUrl
                existing.regulation_name = regulation_name
                existing.save()
                updated_certifications.append(name)
        except Exception as db_error:
            print(f"Error processing certification '{name}': {str(db_error)}")

    return JsonResponse({
        "message": "Successfully pulled Eramba frameworks.",
        "new_certifications": new_certifications,
        "updated_certifications": updated_certifications
    })