"""
Cross-agent comparison module for security findings.
Compares findings across different agents to identify similar security issues.
"""
import os
import uuid
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from dotenv import load_dotenv

from app.database.mongodb_handler import mongodb
from app.models.finding_input import FindingInput
from app.models.finding_db import FindingDB, Status, EvaluatedSeverity
from app.core.finding_deduplication import FindingDeduplication, DEFAULT_SIMILARITY_THRESHOLD

# Load environment variables
load_dotenv()

class CrossAgentComparison:
    """
    Handles comparison of findings between different agents.
    Identifies similar findings across agents and manages their status and attributes.
    Uses similarity comparison to determine if findings are addressing the same issue.
    """
    
    def __init__(self, mongodb_client=None, similarity_threshold=None):
        """
        Initialize the cross-agent comparison handler.
        
        Args:
            mongodb_client: MongoDB client instance (uses global instance if None)
            similarity_threshold: Threshold for considering two findings as similar (0.0-1.0)
                                  If None, reads from SIMILARITY_THRESHOLD env var or uses default
        """
        self.mongodb = mongodb_client or mongodb  # Use global instance if none provided
        
        # Get similarity threshold from param, env var, or default
        if similarity_threshold is None:
            try:
                similarity_threshold = float(os.getenv("SIMILARITY_THRESHOLD", DEFAULT_SIMILARITY_THRESHOLD))
            except (ValueError, TypeError):
                similarity_threshold = DEFAULT_SIMILARITY_THRESHOLD
        
        # Validate similarity threshold
        if not 0.0 <= similarity_threshold <= 1.0:
            raise ValueError("similarity_threshold must be between 0.0 and 1.0")
        self.similarity_threshold = similarity_threshold
        
        # Reuse deduplication module for similarity comparison
        self.deduplication = FindingDeduplication(mongodb_client, similarity_threshold)
    
    async def _get_other_agents_valid_findings(self, project_id: str, exclude_agent_id: str) -> List[FindingDB]:
        """
        Get valid findings (unique_valid or similar_valid) from agents other than the specified one.
        
        Args:
            project_id: Project identifier
            exclude_agent_id: Agent identifier to exclude
            
        Returns:
            List of valid findings from other agents
        """
        # Get all findings for the project
        all_findings = await self.mongodb.get_project_findings(project_id)
        
        # Filter for valid findings from other agents
        valid_statuses = [Status.UNIQUE_VALID, Status.SIMILAR_VALID]
        return [
            finding for finding in all_findings
            if finding.reported_by_agent != exclude_agent_id and finding.status in valid_statuses
        ]
    
    async def _update_finding(self, project_id: str, finding: FindingDB, 
                             updated_fields: Dict[str, Any]) -> FindingDB:
        """
        Helper method to update a finding with new fields.
        
        Args:
            project_id: Project identifier
            finding: Finding to update
            updated_fields: Dictionary of fields to update
            
        Returns:
            Updated finding
        """
        # Create a copy of the original finding data
        finding_data = finding.model_dump()
        
        # Remove fields that will be updated to avoid conflicts
        for field in updated_fields.keys():
            finding_data.pop(field, None)
        
        # Add updated fields
        finding_data.update(updated_fields)
        
        # Create new finding instance
        new_finding = FindingDB(**finding_data)
        
        # Update in database
        await self.mongodb.update_finding(
            project_id,
            finding.finding_id,
            new_finding
        )
        
        return new_finding
    
    async def _mark_as_similar_valid(self, project_id: str, finding: FindingDB, 
                                    similar_to: FindingDB, explanation: str) -> None:
        """
        Mark a finding as similar_valid and inherit category and severity from the similar finding.
        
        Args:
            project_id: Project identifier
            finding: Finding to mark as similar_valid
            similar_to: Similar finding to inherit from
            explanation: Explanation of the similarity
        """
        # Format comment with similarity explanation
        comment = f"Similar to finding {similar_to.finding_id} from agent {similar_to.reported_by_agent}. {explanation}"
        
        # Prepare updated fields
        updated_fields = {
            "status": Status.SIMILAR_VALID,
            "evaluation_comment": comment,
            "updated_at": datetime.utcnow()
        }
        
        # Inherit category and category_id if available
        if getattr(similar_to, 'category', None) is not None:
            updated_fields["category"] = similar_to.category
            
            if getattr(similar_to, 'category_id', None) is not None:
                updated_fields["category_id"] = similar_to.category_id
                
        # Inherit evaluated severity if available
        if getattr(similar_to, 'evaluated_severity', None) is not None:
            updated_fields["evaluated_severity"] = similar_to.evaluated_severity
        
        # Update the finding
        await self._update_finding(project_id, finding, updated_fields)
        
        # If the similar finding is unique_valid, change its status to similar_valid
        if similar_to.status == Status.UNIQUE_VALID:
            # Prepare additional comment
            additional_comment = f"{getattr(similar_to, 'evaluation_comment', '') or ''}\nPart of a similar findings group. Original evaluation maintained."
            
            # Update the similar finding
            await self._update_finding(
                project_id, 
                similar_to, 
                {
                    "status": Status.SIMILAR_VALID,
                    "evaluation_comment": additional_comment,
                    "updated_at": datetime.utcnow()
                }
            )
    
    async def compare_with_other_agents(self, project_id: str, agent_id: str, 
                                      findings: List[FindingDB]) -> Dict[str, Any]:
        """
        Compare a list of findings from one agent with the valid findings from other agents.
        Mark similar findings appropriately and inherit attributes.
        
        Args:
            project_id: Project identifier
            agent_id: Agent identifier for the current findings
            findings: List of findings to compare
            
        Returns:
            Statistics about the comparison results
        """
        # Get valid findings from other agents
        other_valid_findings = await self._get_other_agents_valid_findings(project_id, agent_id)
        
        results = {
            "total": len(findings),
            "similar_valid": 0,
            "pending_evaluation": 0,
            "already_reported": 0,
            "similar_ids": [],
            "pending_ids": []
        }
        
        # Process each finding
        for finding in findings:
            # Skip already reported findings
            if finding.status == Status.ALREADY_REPORTED:
                results["already_reported"] += 1
                continue
                
            similar_valid_finding = None
            highest_similarity = 0
            similarity_explanation = ""
            
            # Compare with valid findings from other agents
            for valid_finding in other_valid_findings:
                similarity_score, explanation = await self.deduplication.compare_findings_with_langchain(
                    finding, valid_finding
                )
                
                # If similarity is above threshold and higher than previous matches
                if similarity_score >= self.similarity_threshold and similarity_score > highest_similarity:
                    highest_similarity = similarity_score
                    similar_valid_finding = valid_finding
                    similarity_explanation = explanation
            
            if similar_valid_finding:
                # Mark as similar_valid and inherit attributes
                await self._mark_as_similar_valid(
                    project_id, 
                    finding, 
                    similar_valid_finding, 
                    similarity_explanation
                )
                results["similar_valid"] += 1
                results["similar_ids"].append(finding.finding_id)
            else:
                # Keep as pending for final evaluation
                results["pending_evaluation"] += 1
                results["pending_ids"].append(finding.finding_id)
        
        return results
    
    async def process_new_findings(self, project_id: str, agent_id: str, 
                                 new_findings: List[FindingInput]) -> Dict[str, Any]:
        """
        Process new findings from an agent through self-deduplication and cross-agent comparison.
        
        Args:
            project_id: Project identifier
            agent_id: Agent identifier
            new_findings: List of new findings to process
            
        Returns:
            Complete processing results
        """
        # Step 1: Self-deduplication - use existing instance
        dedup_results = await self.deduplication.process_findings(project_id, agent_id, new_findings)
        
        # Get created non-duplicate findings
        non_duplicate_findings = []
        for finding_id in dedup_results.get("new_ids", []):
            finding = await self.mongodb.get_finding(project_id, finding_id)
            if finding:
                non_duplicate_findings.append(finding)
        
        # Step 2: Cross-agent comparison
        comparison_results = await self.compare_with_other_agents(
            project_id, agent_id, non_duplicate_findings
        )
        
        # Combine results
        return {
            "deduplication": dedup_results,
            "cross_comparison": comparison_results
        }
    
    async def perform_final_evaluation(self, project_id: str, finding_id: str, 
                                     status: Status, category: Optional[str], 
                                     evaluated_severity: Optional[EvaluatedSeverity], 
                                     evaluation_comment: str) -> Dict[str, Any]:
        """
        Perform final evaluation on a finding, setting status, category, and severity.
        Generates a unique category_id for new categories.
        
        Args:
            project_id: Project identifier
            finding_id: Finding identifier
            status: Final status (usually Status.UNIQUE_VALID or Status.DISPUTED)
            category: Security issue category, None for DISPUTED findings
            evaluated_severity: Evaluated severity level
            evaluation_comment: Evaluation comment
            
        Returns:
            Updated finding information
        """
        # Get current finding
        finding = await self.mongodb.get_finding(project_id, finding_id)
        if not finding:
            return {"error": f"Finding {finding_id} not found"}
        
        # Initialize updated fields with status and comment
        updated_fields = {
            "status": status,
            "evaluation_comment": evaluation_comment,
            "updated_at": datetime.utcnow()
        }
        
        # Set evaluated_severity (can be None for DISPUTED findings)
        updated_fields["evaluated_severity"] = evaluated_severity
        
        # Only set category and category_id for valid findings
        category_id = None
        if category is not None:
            # Get all findings with the same category
            all_findings = await self.mongodb.get_project_findings(project_id)
            category_findings = [f for f in all_findings 
                               if getattr(f, 'category', None) == category 
                               and getattr(f, 'category_id', None) is not None]
            
            # If we have findings with the same category, use their category_id
            if category_findings:
                category_id = category_findings[0].category_id
            
            # If no category_id found, generate a new one
            if not category_id:
                # Generate a unique ID for this category using uuid4
                category_id = f"CAT-{str(uuid.uuid4())[:8]}"
            
            # Update category fields
            updated_fields["category"] = category
            updated_fields["category_id"] = category_id
        else:
            # For DISPUTED findings, explicitly set category and category_id to None
            updated_fields["category"] = None
            updated_fields["category_id"] = None
        
        await self._update_finding(project_id, finding, updated_fields)
        
        return {
            "finding_id": finding_id,
            "status": status,
            "category": category,
            "category_id": category_id,
            "evaluated_severity": evaluated_severity
        } 